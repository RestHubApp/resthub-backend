"""Recorre los 24 diálogos del inventario con un envío válido bajo cada falla."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import httpx

from resthub_chaos import api, ui
from resthub_chaos.config import BACKEND, FRONTEND, OWNER, SEED_CREDENTIAL
from resthub_chaos.scenarios import clear_rules, set_rules

_DATA: dict[str, Any] | None = None


def _name_for(fault: str) -> str:
    """Sin dígitos: varios formularios solo aceptan letras en el nombre."""
    return {
        "500": "Caos quinientos",
        "409": "Caos conflicto",
        "503": "Caos no disponible",
        "timeout": "Caos tiempo",
    }.get(fault, "Caos prueba")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=f"{BACKEND}/api/v1",
        timeout=30,
        headers={"Authorization": f"Bearer {api.token(OWNER)}"},
    )


def _platform_client() -> httpx.Client:
    response = httpx.post(
        f"{BACKEND}/api/v1/auth/login",
        json={"email": "plataforma@resthub.dev", "password": SEED_CREDENTIAL},
        timeout=30,
    )
    response.raise_for_status()
    return httpx.Client(
        base_url=f"{BACKEND}/api/v1",
        timeout=30,
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )


def _served_order(client: httpx.Client, dish: int = 2) -> dict[str, Any]:
    """Un pedido para llevar enviado, listo y servido, creado por el API."""
    created = client.post(
        "/orders",
        json={
            "type": "takeaway",
            "customer_name": "Caos dialogo",
            "client_request_id": uuid.uuid4().hex,
            "items": [{"menu_item_id": dish, "quantity": 1}],
        },
    )
    created.raise_for_status()
    order = created.json()
    for step in ("send", "ready", "served"):
        response = client.post(f"/orders/{order['id']}/{step}")
        response.raise_for_status()
        order = response.json()
    return order


def _paid_order_without_invoice(client: httpx.Client) -> dict[str, Any]:
    """Un pedido servido y cobrado en efectivo exacto, todavía sin boleta ni factura."""
    served = _served_order(client)
    balance = served["balance"]
    charge = client.post(
        f"/orders/{served['id']}/charge",
        json={"payment_method": "cash", "expected_balance": balance, "amount_received": balance},
    )
    charge.raise_for_status()
    return charge.json()


def ensure_data() -> dict[str, Any]:
    """Deja un pedido abierto, una factura, una orden de compra y un turno cerrado."""
    global _DATA
    if _DATA is not None:
        return _DATA
    with _client() as client:
        tables_response = client.get("/tables")
        tables_response.raise_for_status()
        all_tables = tables_response.json()
        free_table = next(
            (m for m in all_tables if m.get("status") == "free" and m.get("active_order") is None),
            None,
        )
        if free_table is None:
            new_table = client.post("/tables", json={"label": f"Caos {uuid.uuid4().hex[:6]}"})
            if new_table.status_code < 300:
                free_table = {"id": new_table.json()["id"]}
        order = client.post(
            "/orders",
            json={
                "type": "dine_in" if free_table else "takeaway",
                "table_id": free_table["id"] if free_table else None,
                "customer_name": "Caos dialogo",
                "client_request_id": uuid.uuid4().hex,
                "items": [{"menu_item_id": 2, "quantity": 1}],
            },
        )
        if order.status_code >= 300:
            active = client.get("/orders/active")
            active.raise_for_status()
            open_orders = [p for p in active.json() if p.get("status") == "open"] or active.json()
            if not open_orders:
                order.raise_for_status()
            target_order = open_orders[0]
        else:
            target_order = order.json()
        paid_response = client.get("/orders", params={"limit": 50, "status": "paid"})
        paid_response.raise_for_status()
        paid = next(
            (p for p in paid_response.json()["items"] if p["status"] == "paid" and p["id"] != 380),
            target_order,
        )
        served_response = client.get("/orders/active")
        served = next((p for p in served_response.json() if p.get("status") == "served"), None)
        invoices = client.get("/billing/invoices", params={"limit": 1})
        invoice_id = None
        if invoices.status_code == 200 and invoices.json().get("items"):
            invoice_id = invoices.json()["items"][0]["id"]
        if invoice_id is None and paid["status"] == "paid":
            issued = client.post(
                "/billing/invoices",
                json={"order_id": paid["id"], "kind": "boleta", "customer_name": "Caos"},
            )
            if issued.status_code < 300:
                invoice_id = issued.json()["id"]
        # Cobrar exige la caja abierta y el diálogo de caja, un turno cerrado; `seed_dev`
        # no deja ninguno de los dos. Se abre aquí y el bloque siguiente la cierra y la
        # vuelve a abrir si todavía no hay un turno cerrado.
        if not client.get("/cash/current").json().get("is_open"):
            client.post(
                "/cash/open", json={"opening_amount": "100.00", "notes": ""}
            ).raise_for_status()
        cash_sessions = client.get("/cash/sessions", params={"limit": 5})
        cash_sessions.raise_for_status()
        closed_session = next((t for t in cash_sessions.json()["items"] if not t["is_open"]), None)
        if closed_session is None and any(t["is_open"] for t in cash_sessions.json()["items"]):
            close_result = client.post(
                "/cash/close", json={"counted_cash": "100.00", "notes": "Arqueo de caos"}
            )
            if close_result.status_code < 300:
                closed_session = close_result.json()
                client.post("/cash/open", json={"opening_amount": "100.00", "notes": ""})
        menu = client.get("/menu")
        menu.raise_for_status()
        dish = 1
        with_options = None
        for category in menu.json().get("categories", []):
            for item in category.get("items", []):
                dish = item["id"]
                if item.get("modifier_groups"):
                    with_options = item["name"]
                    break
            if with_options:
                break
        group = {
            "name": "Término",
            "min_choices": 1,
            "max_choices": 1,
            "options": [{"name": "Jugoso", "price": "0.00"}],
        }
        existing = next(
            (
                item["name"]
                for category in menu.json().get("categories", [])
                for item in category.get("items", [])
                if item["name"] == "Plato caos"
                and item.get("modifier_groups")
                and not item.get("out_of_stock")
            ),
            None,
        )
        if existing:
            with_options = existing
        else:
            category_id = menu.json()["categories"][0]["id"]
            created = client.post(
                "/menu/items",
                json={
                    "category_id": category_id,
                    "name": "Plato caos",
                    "price": "10.00",
                    "modifier_groups": [group],
                },
            )
            if created.status_code < 300:
                with_options = created.json()["name"]
        ingredients = client.get("/inventory/ingredients")
        ingredient_id = None
        if ingredients.status_code == 200 and ingredients.json():
            body = ingredients.json()
            ingredient_id = (body.get("items") if isinstance(body, dict) else body)[0]["id"]
        if ingredient_id is None:
            created = client.post(
                "/inventory/ingredients",
                json={"name": "Arroz caos", "unit": "unit", "min_stock": "1", "unit_cost": "4"},
            )
            if created.status_code < 300:
                ingredient_id = created.json()["id"]
        purchases = client.get("/inventory/purchase-orders", params={"limit": 5})
        to_receive = None
        if purchases.status_code == 200:
            to_receive = next(
                (
                    o
                    for o in purchases.json().get("items", [])
                    if o.get("status") in ("draft", "sent")
                ),
                None,
            )
        if to_receive is None and ingredient_id is not None:
            suppliers = client.get("/inventory/suppliers")
            supplier_id = None
            if suppliers.status_code == 200 and suppliers.json():
                supplier_id = suppliers.json()[0]["id"]
            if supplier_id is None:
                registered = client.post("/inventory/suppliers", json={"name": "Proveedor caos"})
                if registered.status_code < 300:
                    supplier_id = registered.json()["id"]
            if supplier_id is not None:
                new_purchase = client.post(
                    "/inventory/purchase-orders",
                    json={
                        "supplier_id": supplier_id,
                        "lines": [
                            {"ingredient_id": ingredient_id, "quantity": "1", "unit_cost": "4"}
                        ],
                        "notes": "",
                    },
                )
                if new_purchase.status_code < 300:
                    to_receive = new_purchase.json()
        # Una base recién sembrada no trae un pedido servido, y el pagado de
        # arriba puede haber recibido la boleta que se emite para `invoice_id`:
        # los diálogos de cobro y de comprobante usan pedidos propios.
        if served is None:
            served = _served_order(client)
        paid = _paid_order_without_invoice(client)
    with _platform_client() as platform:
        restaurants = platform.get("/platform/restaurants")
        restaurants.raise_for_status()
        restaurant = restaurants.json()["items"][0]["id"]
    _DATA = {
        "order_id": target_order["id"],
        "paid_order_id": paid["id"],
        "served_order_id": served["id"],
        "invoice_id": invoice_id or 1,
        "restaurant_id": restaurant,
        "menu_item_id": dish,
        "modifier_name": with_options,
        "closed_cash": closed_session is not None,
        "purchase_number": None if to_receive is None else to_receive.get("number"),
    }
    return _DATA


def _open_dialog(page: Any) -> Any:
    return page.locator('[data-slot="dialog-content"][data-state="open"]')


def _wait_for_dialog(
    dialog: Any, button: str, seconds: float, field: str | None, value: str | None
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    last: dict[str, Any] = {"abierto": False, "error": [], "guardar_habilitado": False}
    last_read = None
    while time.monotonic() < deadline:
        try:
            is_open = dialog.is_visible()
        except Exception:  # noqa: BLE001
            is_open = False
        alerts = []
        enabled = False
        keeps_value = True
        if is_open:
            alerts = [
                t.strip() for t in dialog.locator('[role="alert"]').all_inner_texts() if t.strip()
            ]
            control = dialog.get_by_role("button", name=button).first
            enabled = control.count() > 0 and control.is_enabled()
            if field is not None and value is not None:
                try:
                    read_value = dialog.get_by_role("textbox", name=field).first.input_value(
                        timeout=1000
                    )
                except Exception:  # noqa: BLE001
                    read_value = dialog.locator("input").first.input_value()
                keeps_value = value in read_value
                last_read = read_value
        last = {
            "abierto": is_open,
            "error": alerts,
            "guardar_habilitado": enabled and keeps_value,
            "leido": last_read if field else None,
        }
        if is_open and alerts and enabled and keeps_value:
            return last
        time.sleep(0.4)
    return last


def _wait_limit(fault: str) -> float:
    # El proxy calla 25 s y Axios reintenta una lectura a los 15 s: el aviso
    # aparece después de los 22 s. Se vuelve en cuanto el diálogo ya cumple.
    return 45 if fault == "timeout" else 8


def _submit(
    page: Any,
    fault: str,
    dialog: Any,
    button: str,
    field: str | None,
    value: str | None,
    method: str = "MUTACION",
) -> dict[str, Any]:
    set_rules(fault, method, "/api/v1/")
    try:
        dialog.get_by_role("button", name=button).first.click()
        result = _wait_for_dialog(dialog, button, _wait_limit(fault), field, value)
    finally:
        clear_rules()
    if not (result["abierto"] and result["error"] and result["guardar_habilitado"]):
        ui.capture(f"04-dialogo-fallo-{button[:12]}-{fault}")
    return result


def _go_to(page: Any, route: str) -> None:
    clear_rules()
    for _ in range(3):
        if page.locator('[data-slot="dialog-content"][data-state="open"]').count() == 0:
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    page.goto(f"{FRONTEND}{route}", wait_until="domcontentloaded", timeout=20000)
    page.locator("main, [data-slot='dialog-content']").first.wait_for(timeout=20000)


def _observe_one(page: Any, fault: str, dialog_id: str) -> dict[str, Any]:
    data = ensure_data()
    try:
        return _DIALOG_SCRIPTS[dialog_id](page, fault, data)
    except Exception as error:  # noqa: BLE001
        clear_rules()
        ui.capture(f"04-dialogo-excepcion-{fault}")
        return {
            "abierto": False,
            "error": [],
            "guardar_habilitado": False,
            "detalle": str(error)[:220],
        }


def _customer_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/clientes")
    page.get_by_role("button", name="Nuevo cliente").click()
    dialog = _open_dialog(page)
    name = _name_for(fault)
    dialog.get_by_label("Nombre", exact=True).fill(name)
    # El alta exige el consentimiento del cliente (Ley N.º 29733): sin él no hay envío.
    dialog.get_by_role("checkbox", name=re.compile("29733")).check()
    return _submit(page, fault, dialog, "Guardar", "Nombre", name, "POST")


def _table_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/mesas")
    page.get_by_role("button", name="Nueva mesa").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nombre").fill(_name_for(fault))
    return _submit(page, fault, dialog, "Guardar", "Nombre", _name_for(fault), "POST")


def _reservation_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/reservas")
    page.get_by_role("button", name="Nueva reserva").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("A nombre de").fill(_name_for(fault))
    if dialog.get_by_label("Personas").input_value() == "":
        dialog.get_by_label("Personas").fill("2")
    return _submit(page, fault, dialog, "Guardar", "A nombre de", _name_for(fault), "POST")


def _supplier_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/inventario?vista=proveedores")
    page.get_by_role("button", name="Nuevo proveedor").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nombre").fill(_name_for(fault))
    return _submit(page, fault, dialog, "Guardar", "Nombre", _name_for(fault), "POST")


def _category_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/menu")
    page.get_by_role("button", name="Nueva categoría").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nombre").fill(_name_for(fault))
    return _submit(page, fault, dialog, "Guardar", "Nombre", _name_for(fault), "POST")


def _dish_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/menu")
    page.get_by_role("button", name="Nuevo plato").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nombre").fill(_name_for(fault))
    dialog.get_by_label("Precio (S/)").fill("12")
    dialog.get_by_label("Categoría").select_option(index=1)
    return _submit(page, fault, dialog, "Crear plato", "Nombre", _name_for(fault))


def _edit_category_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/menu")
    page.get_by_role("button", name="Editar la categoría").first.click()
    dialog = _open_dialog(page)
    actual = dialog.get_by_label("Nombre").input_value()
    return _submit(page, fault, dialog, "Guardar", "Nombre", actual)


def _edit_dish_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    import re

    _go_to(page, "/menu")
    page.get_by_role("button", name=re.compile(r"^Editar (?!la)")).first.click()
    dialog = _open_dialog(page)
    actual = dialog.get_by_label("Nombre").input_value()
    return _submit(page, fault, dialog, "Guardar", "Nombre", actual)


def _waste_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/inventario")
    page.get_by_role("button", name="Merma de").first.click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Cantidad perdida").first.fill("1")
    dialog.get_by_label("Motivo").first.fill(_name_for(fault))
    return _submit(page, fault, dialog, "Registrar merma", "Motivo", _name_for(fault), "POST")


def _account_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/personal")
    page.get_by_role("button", name="Nueva cuenta").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nombre completo").fill(_name_for(fault))
    dialog.get_by_label("Correo").fill(f"caos-{fault}-{uuid.uuid4().hex[:8]}@resthub.dev")
    dialog.get_by_label("Contraseña inicial").fill("resthub123")
    dialog.get_by_label("Rol").select_option(index=1)
    return _submit(page, fault, dialog, "Crear cuenta", "Nombre completo", _name_for(fault), "POST")


def _edit_account_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/personal")
    page.locator("tr").filter(has_text="Mesero").get_by_role("button", name="Editar").click()
    dialog = _open_dialog(page)
    actual = dialog.locator("input").first.input_value()
    return _submit(page, fault, dialog, "Guardar", "Nombre completo", actual)


def _password_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/personal")
    page.get_by_role("button", name="Contraseña").first.click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nueva contraseña").fill("resthub123")
    return _submit(page, fault, dialog, "Restablecer", None, None, "POST")


def _role_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/roles")
    page.get_by_role("button", name="Nuevo rol").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nombre").fill(_name_for(fault))
    checkbox = dialog.get_by_role("checkbox").first
    if checkbox.count() and not checkbox.is_checked():
        checkbox.click()
    return _submit(page, fault, dialog, "Guardar", "Nombre", _name_for(fault), "POST")


def _owner_dialog(page: Any, fault: str, data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, f"/plataforma/restaurantes/{data['restaurant_id']}")
    page.get_by_role("button", name="Agregar encargado").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nombre completo").fill(_name_for(fault))
    dialog.get_by_label("Correo").fill(f"dueno-{fault}-{uuid.uuid4().hex[:8]}@resthub.dev")
    dialog.get_by_label("Contraseña inicial").fill("resthub123")
    return _submit(page, fault, dialog, "Agregar encargado", "Nombre completo", _name_for(fault))


def _cash_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/caja")
    button = page.get_by_role("button", name="Cerró").first
    try:
        button.wait_for(timeout=45000)
    except Exception:  # noqa: BLE001
        retry_button = page.get_by_role("button", name="Reintentar")
        if retry_button.count():
            retry_button.first.click()
        button.wait_for(timeout=45000)
    set_rules(fault, "GET", "/api/v1/")
    button.click()
    dialog = _open_dialog(page)
    try:
        result = _wait_for_dialog(dialog, "Reintentar", _wait_limit(fault), None, None)
    finally:
        clear_rules()
    if not (result["abierto"] and result["error"] and result["guardar_habilitado"]):
        ui.capture(f"04-dialogo-fallo-caja-{fault}")
    return result


def _cancel_dialog(page: Any, fault: str, data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, f"/pedidos/{data['order_id']}")
    page.get_by_role("button", name="Cancelar pedido").click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Motivo").fill(_name_for(fault))
    return _submit(page, fault, dialog, "Cancelar pedido", "Motivo", _name_for(fault), "POST")


def _note_dialog(page: Any, fault: str, data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, f"/pedidos/{data['order_id']}")
    page.get_by_role("button", name="Agregar nota").first.click()
    dialog = _open_dialog(page)
    dialog.get_by_label("Nota para cocina").fill(_name_for(fault))
    return _submit(
        page, fault, dialog, "Guardar nota", "Nota para cocina", _name_for(fault), "PATCH"
    )


def _free_table_label() -> str:
    """El selector solo ofrece mesas libres; si no queda ninguna, crea una."""
    with _client() as client:
        tables_response = client.get("/tables")
        tables_response.raise_for_status()
        all_tables = tables_response.json()
        free_table = next((m for m in all_tables if m.get("status") == "free"), None)
        if free_table is None:
            used = {str(m.get("label")) for m in all_tables}
            label = next(str(n) for n in range(9, 80) if str(n) not in used)
            registered = client.post("/tables", json={"label": label})
            registered.raise_for_status()
            label = str(registered.json().get("label") or label)
        else:
            label = str(free_table["label"])
    return f"Mesa {label}" if label.isdigit() else label


def _move_dialog(page: Any, fault: str, data: dict[str, Any]) -> dict[str, Any]:
    destination = _free_table_label()
    _go_to(page, f"/pedidos/{data['order_id']}")
    page.get_by_role("button", name="Cambiar de mesa").click()
    dialog = _open_dialog(page)
    dialog.get_by_role("button", name=destination).wait_for()
    return _submit(page, fault, dialog, destination, None, None, "POST")


def _takeaway_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/pedidos")
    page.get_by_role("button", name="Para llevar / Delivery").click()
    dialog = _open_dialog(page)
    dialog.get_by_role("radio", name="Delivery").click()
    dialog.get_by_label("Nombre de quien recibe").fill(_name_for(fault))
    # Exacto: el texto del consentimiento también menciona el teléfono.
    dialog.get_by_label("Teléfono", exact=True).fill("987654321")
    dialog.get_by_label("Dirección de entrega").fill("Av. Caos 123")
    # Sin consentimiento el cliente no se guarda y no sale ninguna petición que fallar.
    dialog.get_by_role("checkbox", name=re.compile("29733")).check()
    return _submit(
        page, fault, dialog, "Elegir platos", "Nombre de quien recibe", _name_for(fault), "POST"
    )


def _modifier_dialog(page: Any, fault: str, data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/pedidos/nuevo")
    name = data.get("modifier_name") or "Menú del día"
    page.locator("li").filter(has_text=name).get_by_role("button").first.click()
    dialog = _open_dialog(page)
    dialog.get_by_role("button", name="Jugoso").click()
    return _submit(page, fault, dialog, "Agregar", None, None, "GET")


def _charge_dialog(page: Any, fault: str, data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, f"/pedidos/{data['served_order_id']}")
    page.get_by_role("button", name="Cobrar", exact=True).click()
    dialog = _open_dialog(page)
    amount = dialog.get_by_label("Monto recibido")
    if amount.count():
        amount.fill("50")
    return _submit(page, fault, dialog, "Confirmar pago", "Monto recibido", "50")


def _invoice_dialog(page: Any, fault: str, data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, f"/pedidos/{data['paid_order_id']}")
    page.get_by_role("button", name="Emitir boleta o factura").wait_for()
    page.get_by_role("button", name="Emitir boleta o factura").click()
    dialog = _open_dialog(page)
    return _submit(page, fault, dialog, "Emitir", None, None, "POST")


def _purchase_order_dialog(page: Any, fault: str, _data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/inventario?vista=compras")
    page.get_by_role("button", name="Nueva orden").click()
    dialog = _open_dialog(page)
    dialog.locator("#orden-proveedor").select_option(index=1)
    dialog.get_by_label("Insumo de la línea 1").select_option(index=1)
    dialog.get_by_label("Cantidad", exact=False).first.fill("1")
    dialog.get_by_label("Costo por unidad").fill("4")
    return _submit(page, fault, dialog, "Crear orden", None, None)


def _receive_dialog(page: Any, fault: str, data: dict[str, Any]) -> dict[str, Any]:
    _go_to(page, "/inventario?vista=compras")
    page.get_by_role("button", name="Recibir").first.click()
    dialog = _open_dialog(page)
    return _submit(page, fault, dialog, "Recibir y cargar al stock", None, None, "POST")


_DIALOG_SCRIPTS = {
    "dialogo:cash/CashSessionDialog": _cash_dialog,
    "dialogo:customers/CustomerDialog": _customer_dialog,
    "dialogo:inventory/PurchaseOrderDialog": _purchase_order_dialog,
    "dialogo:inventory/ReceiveOrderDialog": _receive_dialog,
    "dialogo:inventory/StockActionDialog": _waste_dialog,
    "dialogo:inventory/SupplierDialog": _supplier_dialog,
    "dialogo:menu/CategoryHeader": _edit_category_dialog,
    "dialogo:menu/MenuItemRow": _edit_dish_dialog,
    "dialogo:menu/NewMenuDialogs#1": _category_dialog,
    "dialogo:menu/NewMenuDialogs#2": _dish_dialog,
    "dialogo:orders/CancelOrderDialog": _cancel_dialog,
    "dialogo:orders/charge/ChargeContent": _charge_dialog,
    "dialogo:orders/detail/ItemNoteDialog": _note_dialog,
    "dialogo:orders/detail/TablePickerDialog": _move_dialog,
    "dialogo:orders/floor/TakeawayDialog": _takeaway_dialog,
    "dialogo:orders/invoice/InvoiceDialog": _invoice_dialog,
    "dialogo:orders/taking/ModifierDialog": _modifier_dialog,
    "dialogo:platform/OwnersSection": _owner_dialog,
    "dialogo:reservations/ReservationDialog": _reservation_dialog,
    "dialogo:roles/RoleFormDialog": _role_dialog,
    "dialogo:staff/StaffRowActions#1": _edit_account_dialog,
    "dialogo:staff/StaffRowActions#2": _password_dialog,
    "dialogo:staff/StaffView": _account_dialog,
    "dialogo:tables/TableFormDialog": _table_dialog,
}


def observe_dialogs(fault: str) -> dict[str, Any]:
    """Abre cada diálogo, envía datos válidos y exige que siga abierto con error y reintento."""
    import json
    from pathlib import Path

    inventory = json.loads(
        (Path.home() / "github/wt-pruebas-e2e-front/e2e/cobertura-ui.json").read_text()
    )
    page = ui._page()
    page.set_default_timeout(20000)
    clear_rules()
    # Tras el recorrido con timeout quedan peticiones retenidas 25 s en el proxy.
    if fault == "timeout":
        time.sleep(26)
    results = {}
    for element in inventory["elementos"]:
        if element["tipo"] != "dialogo":
            continue
        results[element["id"]] = _observe_one(page, fault, element["id"])
        try:
            if page.get_by_role("dialog").count():
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
    clear_rules()
    api._record(f"dialogos_{fault}", results)
    pass_count = sum(
        bool(r.get("abierto") and r.get("error") and r.get("guardar_habilitado"))
        for r in results.values()
    )
    return {"falla": fault, "total": len(results), "cumplen": pass_count}
