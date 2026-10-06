"""Recorre los 24 diálogos del inventario con un envío válido bajo cada falla."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import httpx

from resthub_chaos import api, ui
from resthub_chaos.config import BACKEND, CLAVE, ENCARGADO, FRONTEND
from resthub_chaos.escenarios import limpiar, reglas

_DATOS: dict[str, Any] | None = None


def _nombre(falla: str) -> str:
    """Sin dígitos: varios formularios solo aceptan letras en el nombre."""
    return {
        "500": "Caos quinientos",
        "409": "Caos conflicto",
        "503": "Caos no disponible",
        "timeout": "Caos tiempo",
    }.get(falla, "Caos prueba")


def _cliente() -> httpx.Client:
    return httpx.Client(
        base_url=f"{BACKEND}/api/v1",
        timeout=30,
        headers={"Authorization": f"Bearer {api.token(ENCARGADO)}"},
    )


def _plataforma() -> httpx.Client:
    respuesta = httpx.post(
        f"{BACKEND}/api/v1/platform/auth/login",
        json={"email": "plataforma@resthub.dev", "password": CLAVE},
        timeout=30,
    )
    respuesta.raise_for_status()
    return httpx.Client(
        base_url=f"{BACKEND}/api/v1",
        timeout=30,
        headers={"Authorization": f"Bearer {respuesta.json()['access_token']}"},
    )


def _pedido_servido(cliente: httpx.Client, plato: int = 2) -> dict[str, Any]:
    """Un pedido para llevar enviado, listo y servido, creado por el API."""
    creado = cliente.post(
        "/orders",
        json={
            "type": "takeaway",
            "customer_name": "Caos dialogo",
            "client_request_id": uuid.uuid4().hex,
            "items": [{"menu_item_id": plato, "quantity": 1}],
        },
    )
    creado.raise_for_status()
    pedido = creado.json()
    for paso in ("send", "ready", "served"):
        respuesta = cliente.post(f"/orders/{pedido['id']}/{paso}")
        respuesta.raise_for_status()
        pedido = respuesta.json()
    return pedido


def _pedido_pagado_sin_comprobante(cliente: httpx.Client) -> dict[str, Any]:
    """Un pedido servido y cobrado en efectivo exacto, todavía sin boleta ni factura."""
    servido = _pedido_servido(cliente)
    saldo = servido["balance"]
    cobro = cliente.post(
        f"/orders/{servido['id']}/charge",
        json={"payment_method": "cash", "expected_balance": saldo, "amount_received": saldo},
    )
    cobro.raise_for_status()
    return cobro.json()


def asegurar_datos() -> dict[str, Any]:
    """Deja un pedido abierto, una factura, una orden de compra y un turno cerrado."""
    global _DATOS
    if _DATOS is not None:
        return _DATOS
    with _cliente() as cliente:
        mesas = cliente.get("/tables")
        mesas.raise_for_status()
        lista = mesas.json()
        libre = next(
            (m for m in lista if m.get("status") == "free" and m.get("active_order") is None), None
        )
        if libre is None:
            alta_mesa = cliente.post("/tables", json={"label": f"Caos {uuid.uuid4().hex[:6]}"})
            if alta_mesa.status_code < 300:
                libre = {"id": alta_mesa.json()["id"]}
        pedido = cliente.post(
            "/orders",
            json={
                "type": "dine_in" if libre else "takeaway",
                "table_id": libre["id"] if libre else None,
                "customer_name": "Caos dialogo",
                "client_request_id": uuid.uuid4().hex,
                "items": [{"menu_item_id": 2, "quantity": 1}],
            },
        )
        if pedido.status_code >= 300:
            activos = cliente.get("/orders/active")
            activos.raise_for_status()
            abiertos = [p for p in activos.json() if p.get("status") == "open"] or activos.json()
            if not abiertos:
                pedido.raise_for_status()
            orden = abiertos[0]
        else:
            orden = pedido.json()
        pagados = cliente.get("/orders", params={"limit": 50, "status": "paid"})
        pagados.raise_for_status()
        pagado = next(
            (p for p in pagados.json()["items"] if p["status"] == "paid" and p["id"] != 380), orden
        )
        servidos = cliente.get("/orders/active")
        servido = next((p for p in servidos.json() if p.get("status") == "served"), None)
        facturas = cliente.get("/billing/invoices", params={"limit": 1})
        factura_id = None
        if facturas.status_code == 200 and facturas.json().get("items"):
            factura_id = facturas.json()["items"][0]["id"]
        if factura_id is None and pagado["status"] == "paid":
            emitida = cliente.post(
                "/billing/invoices",
                json={"order_id": pagado["id"], "kind": "boleta", "customer_name": "Caos"},
            )
            if emitida.status_code < 300:
                factura_id = emitida.json()["id"]
        # Cobrar exige la caja abierta y el diálogo de caja, un turno cerrado; `seed_dev`
        # no deja ninguno de los dos. Se abre aquí y el bloque siguiente la cierra y la
        # vuelve a abrir si todavía no hay un turno cerrado.
        if not cliente.get("/cash/current").json().get("is_open"):
            cliente.post(
                "/cash/open", json={"opening_amount": "100.00", "notes": ""}
            ).raise_for_status()
        caja = cliente.get("/cash/sessions", params={"limit": 5})
        caja.raise_for_status()
        cerrada = next((t for t in caja.json()["items"] if not t["is_open"]), None)
        if cerrada is None and any(t["is_open"] for t in caja.json()["items"]):
            cierre = cliente.post(
                "/cash/close", json={"counted_cash": "100.00", "notes": "Arqueo de caos"}
            )
            if cierre.status_code < 300:
                cerrada = cierre.json()
                cliente.post("/cash/open", json={"opening_amount": "100.00", "notes": ""})
        menu = cliente.get("/menu")
        menu.raise_for_status()
        plato = 1
        con_opciones = None
        for categoria in menu.json().get("categories", []):
            for item in categoria.get("items", []):
                plato = item["id"]
                if item.get("modifier_groups"):
                    con_opciones = item["name"]
                    break
            if con_opciones:
                break
        grupo = {
            "name": "Término",
            "min_choices": 1,
            "max_choices": 1,
            "options": [{"name": "Jugoso", "price": "0.00"}],
        }
        if con_opciones is None or True:
            existente = next(
                (
                    item["name"]
                    for categoria in menu.json().get("categories", [])
                    for item in categoria.get("items", [])
                    if item["name"] == "Plato caos"
                    and item.get("modifier_groups")
                    and not item.get("out_of_stock")
                ),
                None,
            )
            if existente:
                con_opciones = existente
            else:
                categoria_id = menu.json()["categories"][0]["id"]
                creado = cliente.post(
                    "/menu/items",
                    json={
                        "category_id": categoria_id,
                        "name": "Plato caos",
                        "price": "10.00",
                        "modifier_groups": [grupo],
                    },
                )
                if creado.status_code < 300:
                    con_opciones = creado.json()["name"]
        insumos = cliente.get("/inventory/ingredients")
        insumo_id = None
        if insumos.status_code == 200 and insumos.json():
            cuerpo = insumos.json()
            insumo_id = (cuerpo.get("items") if isinstance(cuerpo, dict) else cuerpo)[0]["id"]
        if insumo_id is None:
            creado = cliente.post(
                "/inventory/ingredients",
                json={"name": "Arroz caos", "unit": "unit", "min_stock": "1", "unit_cost": "4"},
            )
            if creado.status_code < 300:
                insumo_id = creado.json()["id"]
        compras = cliente.get("/inventory/purchase-orders", params={"limit": 5})
        recibir = None
        if compras.status_code == 200:
            recibir = next(
                (
                    o
                    for o in compras.json().get("items", [])
                    if o.get("status") in ("draft", "sent")
                ),
                None,
            )
        if recibir is None and insumo_id is not None:
            proveedores = cliente.get("/inventory/suppliers")
            proveedor_id = None
            if proveedores.status_code == 200 and proveedores.json():
                proveedor_id = proveedores.json()[0]["id"]
            if proveedor_id is None:
                alta = cliente.post("/inventory/suppliers", json={"name": "Proveedor caos"})
                if alta.status_code < 300:
                    proveedor_id = alta.json()["id"]
            if proveedor_id is not None:
                nueva = cliente.post(
                    "/inventory/purchase-orders",
                    json={
                        "supplier_id": proveedor_id,
                        "lines": [{"ingredient_id": insumo_id, "quantity": "1", "unit_cost": "4"}],
                        "notes": "",
                    },
                )
                if nueva.status_code < 300:
                    recibir = nueva.json()
        # Una base recién sembrada no trae un pedido servido, y el pagado de
        # arriba puede haber recibido la boleta que se emite para `invoice_id`:
        # los diálogos de cobro y de comprobante usan pedidos propios.
        if servido is None:
            servido = _pedido_servido(cliente)
        pagado = _pedido_pagado_sin_comprobante(cliente)
    with _plataforma() as plataforma:
        restaurantes = plataforma.get("/platform/restaurants")
        restaurantes.raise_for_status()
        restaurante = restaurantes.json()["items"][0]["id"]
    _DATOS = {
        "order_id": orden["id"],
        "paid_order_id": pagado["id"],
        "served_order_id": servido["id"],
        "invoice_id": factura_id or 1,
        "restaurant_id": restaurante,
        "menu_item_id": plato,
        "modifier_name": con_opciones,
        "closed_cash": cerrada is not None,
        "purchase_number": None if recibir is None else recibir.get("number"),
    }
    return _DATOS


def _abierto(pagina: Any) -> Any:
    return pagina.locator('[data-slot="dialog-content"][data-state="open"]')


def _esperar(
    dialogo: Any, boton: str, segundos: float, campo: str | None, valor: str | None
) -> dict[str, Any]:
    limite = time.monotonic() + segundos
    ultimo: dict[str, Any] = {"abierto": False, "error": [], "guardar_habilitado": False}
    ultimo_leido = None
    while time.monotonic() < limite:
        try:
            abierto = dialogo.is_visible()
        except Exception:  # noqa: BLE001
            abierto = False
        alertas = []
        habilitado = False
        conserva = True
        if abierto:
            alertas = [
                t.strip() for t in dialogo.locator('[role="alert"]').all_inner_texts() if t.strip()
            ]
            control = dialogo.get_by_role("button", name=boton).first
            habilitado = control.count() > 0 and control.is_enabled()
            if campo is not None and valor is not None:
                try:
                    leido = dialogo.get_by_role("textbox", name=campo).first.input_value(
                        timeout=1000
                    )
                except Exception:  # noqa: BLE001
                    leido = dialogo.locator("input").first.input_value()
                conserva = valor in leido
                ultimo_leido = leido
        ultimo = {
            "abierto": abierto,
            "error": alertas,
            "guardar_habilitado": habilitado and conserva,
            "leido": ultimo_leido if campo else None,
        }
        if abierto and alertas and habilitado and conserva:
            return ultimo
        time.sleep(0.4)
    return ultimo


def _plazo(falla: str) -> float:
    # El proxy calla 25 s y Axios reintenta una lectura a los 15 s: el aviso
    # aparece después de los 22 s. Se vuelve en cuanto el diálogo ya cumple.
    return 45 if falla == "timeout" else 8


def _enviar(
    pagina: Any,
    falla: str,
    dialogo: Any,
    boton: str,
    campo: str | None,
    valor: str | None,
    metodo: str = "MUTACION",
) -> dict[str, Any]:
    reglas(falla, metodo, "/api/v1/")
    try:
        dialogo.get_by_role("button", name=boton).first.click()
        resultado = _esperar(dialogo, boton, _plazo(falla), campo, valor)
    finally:
        limpiar()
    if not (resultado["abierto"] and resultado["error"] and resultado["guardar_habilitado"]):
        ui.capturar(f"04-dialogo-fallo-{boton[:12]}-{falla}")
    return resultado


def _ir(pagina: Any, ruta: str) -> None:
    limpiar()
    for _ in range(3):
        if pagina.locator('[data-slot="dialog-content"][data-state="open"]').count() == 0:
            break
        pagina.keyboard.press("Escape")
        pagina.wait_for_timeout(200)
    pagina.goto(f"{FRONTEND}{ruta}", wait_until="domcontentloaded", timeout=20000)
    pagina.locator("main, [data-slot='dialog-content']").first.wait_for(timeout=20000)


def _observar_uno(pagina: Any, falla: str, dialogo_id: str) -> dict[str, Any]:
    datos = asegurar_datos()
    try:
        return _RECETAS[dialogo_id](pagina, falla, datos)
    except Exception as error:  # noqa: BLE001
        limpiar()
        ui.capturar(f"04-dialogo-excepcion-{falla}")
        return {
            "abierto": False,
            "error": [],
            "guardar_habilitado": False,
            "detalle": str(error)[:220],
        }


def _cliente_dialogo(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/clientes")
    pagina.get_by_role("button", name="Nuevo cliente").click()
    dialogo = _abierto(pagina)
    nombre = _nombre(falla)
    dialogo.get_by_label("Nombre", exact=True).fill(nombre)
    # El alta exige el consentimiento del cliente (Ley N.º 29733): sin él no hay envío.
    dialogo.get_by_role("checkbox", name=re.compile("29733")).check()
    return _enviar(pagina, falla, dialogo, "Guardar", "Nombre", nombre, "POST")


def _mesa(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/mesas")
    pagina.get_by_role("button", name="Nueva mesa").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nombre").fill(_nombre(falla))
    return _enviar(pagina, falla, dialogo, "Guardar", "Nombre", _nombre(falla), "POST")


def _reserva(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/reservas")
    pagina.get_by_role("button", name="Nueva reserva").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("A nombre de").fill(_nombre(falla))
    if dialogo.get_by_label("Personas").input_value() == "":
        dialogo.get_by_label("Personas").fill("2")
    return _enviar(pagina, falla, dialogo, "Guardar", "A nombre de", _nombre(falla), "POST")


def _proveedor(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/inventario?vista=proveedores")
    pagina.get_by_role("button", name="Nuevo proveedor").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nombre").fill(_nombre(falla))
    return _enviar(pagina, falla, dialogo, "Guardar", "Nombre", _nombre(falla), "POST")


def _categoria(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/menu")
    pagina.get_by_role("button", name="Nueva categoría").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nombre").fill(_nombre(falla))
    return _enviar(pagina, falla, dialogo, "Guardar", "Nombre", _nombre(falla), "POST")


def _plato(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/menu")
    pagina.get_by_role("button", name="Nuevo plato").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nombre").fill(_nombre(falla))
    dialogo.get_by_label("Precio (S/)").fill("12")
    dialogo.get_by_label("Categoría").select_option(index=1)
    return _enviar(pagina, falla, dialogo, "Crear plato", "Nombre", _nombre(falla))


def _editar_categoria(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/menu")
    pagina.get_by_role("button", name="Editar la categoría").first.click()
    dialogo = _abierto(pagina)
    actual = dialogo.get_by_label("Nombre").input_value()
    return _enviar(pagina, falla, dialogo, "Guardar", "Nombre", actual)


def _editar_plato(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    import re

    _ir(pagina, "/menu")
    pagina.get_by_role("button", name=re.compile(r"^Editar (?!la)")).first.click()
    dialogo = _abierto(pagina)
    actual = dialogo.get_by_label("Nombre").input_value()
    return _enviar(pagina, falla, dialogo, "Guardar", "Nombre", actual)


def _merma(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/inventario")
    pagina.get_by_role("button", name="Merma de").first.click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Cantidad perdida").first.fill("1")
    dialogo.get_by_label("Motivo").first.fill(_nombre(falla))
    return _enviar(pagina, falla, dialogo, "Registrar merma", "Motivo", _nombre(falla), "POST")


def _cuenta(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/personal")
    pagina.get_by_role("button", name="Nueva cuenta").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nombre completo").fill(_nombre(falla))
    dialogo.get_by_label("Correo").fill(f"caos-{falla}-{uuid.uuid4().hex[:8]}@resthub.dev")
    dialogo.get_by_label("Contraseña inicial").fill("resthub123")
    dialogo.get_by_label("Rol").select_option(index=1)
    return _enviar(
        pagina, falla, dialogo, "Crear cuenta", "Nombre completo", _nombre(falla), "POST"
    )


def _editar_cuenta(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/personal")
    pagina.locator("tr").filter(has_text="Mesero").get_by_role("button", name="Editar").click()
    dialogo = _abierto(pagina)
    actual = dialogo.locator("input").first.input_value()
    return _enviar(pagina, falla, dialogo, "Guardar", "Nombre completo", actual)


def _clave(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/personal")
    pagina.get_by_role("button", name="Contraseña").first.click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nueva contraseña").fill("resthub123")
    return _enviar(pagina, falla, dialogo, "Restablecer", None, None, "POST")


def _rol(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/roles")
    pagina.get_by_role("button", name="Nuevo rol").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nombre").fill(_nombre(falla))
    caja = dialogo.get_by_role("checkbox").first
    if caja.count() and not caja.is_checked():
        caja.click()
    return _enviar(pagina, falla, dialogo, "Guardar", "Nombre", _nombre(falla), "POST")


def _encargado(pagina: Any, falla: str, datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, f"/plataforma/restaurantes/{datos['restaurant_id']}")
    pagina.get_by_role("button", name="Agregar encargado").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nombre completo").fill(_nombre(falla))
    dialogo.get_by_label("Correo").fill(f"dueno-{falla}-{uuid.uuid4().hex[:8]}@resthub.dev")
    dialogo.get_by_label("Contraseña inicial").fill("resthub123")
    return _enviar(pagina, falla, dialogo, "Agregar encargado", "Nombre completo", _nombre(falla))


def _caja(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/caja")
    boton = pagina.get_by_role("button", name="Cerró").first
    try:
        boton.wait_for(timeout=45000)
    except Exception:  # noqa: BLE001
        reintento = pagina.get_by_role("button", name="Reintentar")
        if reintento.count():
            reintento.first.click()
        boton.wait_for(timeout=45000)
    reglas(falla, "GET", "/api/v1/")
    boton.click()
    dialogo = _abierto(pagina)
    try:
        resultado = _esperar(dialogo, "Reintentar", _plazo(falla), None, None)
    finally:
        limpiar()
    if not (resultado["abierto"] and resultado["error"] and resultado["guardar_habilitado"]):
        ui.capturar(f"04-dialogo-fallo-caja-{falla}")
    return resultado


def _cancelar(pagina: Any, falla: str, datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, f"/pedidos/{datos['order_id']}")
    pagina.get_by_role("button", name="Cancelar pedido").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Motivo").fill(_nombre(falla))
    return _enviar(pagina, falla, dialogo, "Cancelar pedido", "Motivo", _nombre(falla), "POST")


def _nota(pagina: Any, falla: str, datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, f"/pedidos/{datos['order_id']}")
    pagina.get_by_role("button", name="Agregar nota").first.click()
    dialogo = _abierto(pagina)
    dialogo.get_by_label("Nota para cocina").fill(_nombre(falla))
    return _enviar(
        pagina, falla, dialogo, "Guardar nota", "Nota para cocina", _nombre(falla), "PATCH"
    )


def _mesa_libre() -> str:
    """El selector solo ofrece mesas libres; si no queda ninguna, crea una."""
    with _cliente() as cliente:
        mesas = cliente.get("/tables")
        mesas.raise_for_status()
        lista = mesas.json()
        libre = next((m for m in lista if m.get("status") == "free"), None)
        if libre is None:
            usadas = {str(m.get("label")) for m in lista}
            etiqueta = next(str(n) for n in range(9, 80) if str(n) not in usadas)
            alta = cliente.post("/tables", json={"label": etiqueta})
            alta.raise_for_status()
            etiqueta = str(alta.json().get("label") or etiqueta)
        else:
            etiqueta = str(libre["label"])
    return f"Mesa {etiqueta}" if etiqueta.isdigit() else etiqueta


def _mover(pagina: Any, falla: str, datos: dict[str, Any]) -> dict[str, Any]:
    destino = _mesa_libre()
    _ir(pagina, f"/pedidos/{datos['order_id']}")
    pagina.get_by_role("button", name="Cambiar de mesa").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_role("button", name=destino).wait_for()
    return _enviar(pagina, falla, dialogo, destino, None, None, "POST")


def _llevar(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/pedidos")
    pagina.get_by_role("button", name="Para llevar / Delivery").click()
    dialogo = _abierto(pagina)
    dialogo.get_by_role("radio", name="Delivery").click()
    dialogo.get_by_label("Nombre de quien recibe").fill(_nombre(falla))
    # Exacto: el texto del consentimiento también menciona el teléfono.
    dialogo.get_by_label("Teléfono", exact=True).fill("987654321")
    dialogo.get_by_label("Dirección de entrega").fill("Av. Caos 123")
    # Sin consentimiento el cliente no se guarda y no sale ninguna petición que fallar.
    dialogo.get_by_role("checkbox", name=re.compile("29733")).check()
    return _enviar(
        pagina, falla, dialogo, "Elegir platos", "Nombre de quien recibe", _nombre(falla), "POST"
    )


def _modificador(pagina: Any, falla: str, datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/pedidos/nuevo")
    nombre = datos.get("modifier_name") or "Menú del día"
    pagina.locator("li").filter(has_text=nombre).get_by_role("button").first.click()
    dialogo = _abierto(pagina)
    dialogo.get_by_role("button", name="Jugoso").click()
    return _enviar(pagina, falla, dialogo, "Agregar", None, None, "GET")


def _cobro(pagina: Any, falla: str, datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, f"/pedidos/{datos['served_order_id']}")
    pagina.get_by_role("button", name="Cobrar", exact=True).click()
    dialogo = _abierto(pagina)
    monto = dialogo.get_by_label("Monto recibido")
    if monto.count():
        monto.fill("50")
    return _enviar(pagina, falla, dialogo, "Confirmar pago", "Monto recibido", "50")


def _factura(pagina: Any, falla: str, datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, f"/pedidos/{datos['paid_order_id']}")
    pagina.get_by_role("button", name="Emitir boleta o factura").wait_for()
    pagina.get_by_role("button", name="Emitir boleta o factura").click()
    dialogo = _abierto(pagina)
    return _enviar(pagina, falla, dialogo, "Emitir", None, None, "POST")


def _orden_compra(pagina: Any, falla: str, _datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/inventario?vista=compras")
    pagina.get_by_role("button", name="Nueva orden").click()
    dialogo = _abierto(pagina)
    dialogo.locator("#orden-proveedor").select_option(index=1)
    dialogo.get_by_label("Insumo de la línea 1").select_option(index=1)
    dialogo.get_by_label("Cantidad", exact=False).first.fill("1")
    dialogo.get_by_label("Costo por unidad").fill("4")
    return _enviar(pagina, falla, dialogo, "Crear orden", None, None)


def _recibir(pagina: Any, falla: str, datos: dict[str, Any]) -> dict[str, Any]:
    _ir(pagina, "/inventario?vista=compras")
    pagina.get_by_role("button", name="Recibir").first.click()
    dialogo = _abierto(pagina)
    return _enviar(pagina, falla, dialogo, "Recibir y cargar al stock", None, None, "POST")


_RECETAS = {
    "dialogo:cash/CashSessionDialog": _caja,
    "dialogo:customers/CustomerDialog": _cliente_dialogo,
    "dialogo:inventory/PurchaseOrderDialog": _orden_compra,
    "dialogo:inventory/ReceiveOrderDialog": _recibir,
    "dialogo:inventory/StockActionDialog": _merma,
    "dialogo:inventory/SupplierDialog": _proveedor,
    "dialogo:menu/CategoryHeader": _editar_categoria,
    "dialogo:menu/MenuItemRow": _editar_plato,
    "dialogo:menu/NewMenuDialogs#1": _categoria,
    "dialogo:menu/NewMenuDialogs#2": _plato,
    "dialogo:orders/CancelOrderDialog": _cancelar,
    "dialogo:orders/charge/ChargeContent": _cobro,
    "dialogo:orders/detail/ItemNoteDialog": _nota,
    "dialogo:orders/detail/TablePickerDialog": _mover,
    "dialogo:orders/floor/TakeawayDialog": _llevar,
    "dialogo:orders/invoice/InvoiceDialog": _factura,
    "dialogo:orders/taking/ModifierDialog": _modificador,
    "dialogo:platform/OwnersSection": _encargado,
    "dialogo:reservations/ReservationDialog": _reserva,
    "dialogo:roles/RoleFormDialog": _rol,
    "dialogo:staff/StaffRowActions#1": _editar_cuenta,
    "dialogo:staff/StaffRowActions#2": _clave,
    "dialogo:staff/StaffView": _cuenta,
    "dialogo:tables/TableFormDialog": _mesa,
}


def observar_dialogos(falla: str) -> dict[str, Any]:
    """Abre cada diálogo, envía datos válidos y exige que siga abierto con error y reintento."""
    import json
    from pathlib import Path

    inventario = json.loads(
        (Path.home() / "github/wt-pruebas-e2e-front/e2e/cobertura-ui.json").read_text()
    )
    pagina = ui._pagina()
    pagina.set_default_timeout(20000)
    limpiar()
    # Tras el recorrido con timeout quedan peticiones retenidas 25 s en el proxy.
    if falla == "timeout":
        time.sleep(26)
    resultados = {}
    for elemento in inventario["elementos"]:
        if elemento["tipo"] != "dialogo":
            continue
        resultados[elemento["id"]] = _observar_uno(pagina, falla, elemento["id"])
        try:
            if pagina.get_by_role("dialog").count():
                pagina.keyboard.press("Escape")
                pagina.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
    limpiar()
    api._anotar(f"dialogos_{falla}", resultados)
    cumplen = sum(
        bool(r.get("abierto") and r.get("error") and r.get("guardar_habilitado"))
        for r in resultados.values()
    )
    return {"falla": falla, "total": len(resultados), "cumplen": cumplen}
