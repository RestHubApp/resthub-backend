"""Escenarios UI observables de caída del proceso, fallas HTTP y corte SSE."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from resthub_chaos import api, database, ui
from resthub_chaos.config import EVIDENCE, FAULT_PROXY, FRONTEND, RUN, SEED_CREDENTIAL, WAITER

INVENTORY = Path.home() / "github/wt-pruebas-e2e-front/e2e/cobertura-ui.json"


def set_rules(
    fault: str = "500", method: str = "GET", route: str = "/api/v1/", times: int | None = None
) -> dict:
    response = httpx.post(
        f"{FAULT_PROXY}/__caos/reglas",
        json={
            "reglas": [
                {"fault": fault, "method": method, "route": route, "times": times, "wait_s": 25}
            ]
        },
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


def clear_rules() -> dict:
    return httpx.delete(f"{FAULT_PROXY}/__caos/reglas", timeout=5).json()


def release_sse() -> dict:
    return httpx.post(f"{FAULT_PROXY}/__caos/sse/liberar", timeout=5).json()


def freeze_sse() -> dict:
    return httpx.post(f"{FAULT_PROXY}/__caos/sse/congelar", timeout=5).json()


def _wait_for_navigation(route: str, seconds: float = 8) -> dict[str, Any]:
    page = ui._page()
    page.goto(f"{FRONTEND}{route}", wait_until="commit")
    time.sleep(seconds)
    return ui.screen_state()


def open_platform() -> str:
    page = ui._page()
    page.goto(f"{FRONTEND}/plataforma/acceso")
    page.get_by_label("Correo").fill("plataforma@resthub.dev")
    page.get_by_label("Contraseña", exact=True).fill(SEED_CREDENTIAL)
    page.get_by_role("button", name="Entrar").click()
    page.wait_for_url(lambda url: urlparse(url).path == "/plataforma", timeout=20000)
    return page.url


def prepare_order() -> dict[str, Any]:
    ui.open_session(WAITER, "/pedidos/nuevo?tipo=llevar&cliente=Caos")
    page = ui._page()
    page.get_by_role("button", name="Agregar", exact=False).first.click(timeout=10000)
    button = page.get_by_role("button", name="Enviar a cocina")
    if button.count() == 0:
        page.get_by_role("button", name="Ver pedido", exact=False).first.click()
    return {"url": page.url, "texto": page.locator("main").inner_text()[:350]}


def submit_with_backend_down() -> dict[str, Any]:
    page = ui._page()
    page.get_by_role("button", name="Enviar a cocina").first.click()
    page.wait_for_timeout(18000)
    observation = ui.screen_state()
    observation["texto"] = page.locator("body").inner_text()[-700:]
    observation["cola"] = page.evaluate(
        "() => localStorage.getItem('resthub.pedidos-sin-enviar.v2')"
    )
    ui.capture("03-backend-caido")
    api._record("pedido_sin_backend", observation)
    return observation


def retry_after_start() -> dict[str, Any]:
    page = ui._page()
    page.goto(f"{FRONTEND}/pedidos")
    page.wait_for_timeout(2000)
    button = page.get_by_role("button", name="Reintentar", exact=False)
    if button.count():
        button.first.click()
    page.wait_for_timeout(5000)
    ui.capture("03-pedido-recuperado")
    previous = api.observations().get("pedido_sin_backend", {})
    queue = json.loads(previous.get("cola") or "{}")
    # La estructura de la cola puede variar; registrar el contenido crudo y
    # consultar cada id de cliente por separado, nunca inferir duplicados de UI.
    crids: list[str] = []

    def find_crids(value: Any) -> None:
        if isinstance(value, dict):
            crids.extend(str(v) for k, v in value.items() if k == "client_request_id")
            for v in value.values():
                find_crids(v)
        elif isinstance(value, list):
            for v in value:
                find_crids(v)

    find_crids(queue)
    resends = []
    for entry_item in queue if isinstance(queue, list) else []:
        body = entry_item.get("request", {})
        if body.get("client_request_id"):
            response = httpx.post(
                f"{api.API}/orders",
                json=body,
                headers={"Authorization": f"Bearer {api.token(WAITER)}"},
                timeout=15,
            )
            resends.append({"estado": response.status_code, "pedido": response.json().get("id")})
    with ThreadPoolExecutor(max_workers=1) as pool:
        matches = {c: pool.submit(database.orders_with_client_id, c).result() for c in crids}
    result = {
        "crids": crids,
        "reenvios": resends,
        "coincidencias": matches,
        "pantalla": ui.screen_state(),
    }
    api._record("pedido_recuperado", result)
    return result


def exact_order() -> bool:
    entry = api.observations().get("pedido_recuperado")
    return entry is None or (
        bool(entry["crids"]) and all(len(v) == 1 for v in entry["coincidencias"].values())
    )


def _screen_recoverable(state: dict[str, Any]) -> bool:
    return bool(
        (state.get("alertas") or state.get("avisos"))
        and state.get("reintentar")
        and not state.get("blanco")
        and not state.get("cargando")
        and "error_prueba" not in state
    )


def _real_ids() -> dict[str, str]:
    """Identificadores que ya existen, leídos del backend directo (sin el proxy de fallas)."""
    from resthub_chaos.dialogs import ensure_data

    data = ensure_data()
    return {
        ":orderId": str(data["order_id"]),
        ":invoiceId": str(data["invoice_id"]),
        ":restaurantId": str(data["restaurant_id"]),
        ":menuItemId": str(data["menu_item_id"]),
    }


def _destinations(route: str, ids: dict[str, str]) -> list[str]:
    common = route.replace(":kind", "comanda").replace("*", "inexistente")
    fake = common
    for token in (":orderId", ":invoiceId", ":restaurantId", ":menuItemId"):
        fake = fake.replace(token, "99999999")
    visits = [fake]
    if not any(token in route for token in ids):
        return visits
    real = common
    for token, value in ids.items():
        real = real.replace(token, value)
    if real != fake:
        visits.append(real)
    return visits


def _wait_for_screen(seconds: float) -> dict[str, Any]:
    """Espera a que los reintentos terminen y quede un error con forma de reintentar."""
    deadline = time.monotonic() + seconds
    state = ui.screen_state()
    while time.monotonic() < deadline:
        state = ui.screen_state()
        if (
            (state.get("alertas") or state.get("avisos"))
            and state.get("reintentar")
            and not state.get("blanco")
            and not state.get("cargando")
        ):
            return state
        time.sleep(0.5)
    return state


def observe_routes(fault: str, duration_s: float = 2.0) -> dict[str, Any]:
    """Recorre cada ruta con un id inexistente y, si es parametrizada, también con uno real."""
    inventory = json.loads(INVENTORY.read_text())
    routes = [e["id"].removeprefix("ruta:") for e in inventory["elementos"] if e["tipo"] == "ruta"]
    ids = _real_ids()
    page = ui._page()
    # El 409 no se reintenta; el 500 y el 503 sí, una vez. El timeout agota dos plazos de 15 s.
    wait = duration_s if duration_s > 20 else (45.0 if fault == "timeout" else 12.0)
    results = []
    for route in routes:
        visits = _destinations(route, ids)
        states = []
        for destination in visits:
            try:
                page.goto(f"{FRONTEND}{destination}", wait_until="commit", timeout=12000)
                state = _wait_for_screen(wait)
                text = page.locator("body").inner_text()[-350:]
                states.append({"destino": destination, **state, "texto": text})
            except Exception as error:  # noqa: BLE001 - cada ruta queda en la matriz aunque falle
                states.append(
                    {
                        "destino": destination,
                        "error_prueba": str(error)[:250],
                        "alertas": [],
                        "reintentar": False,
                        "blanco": True,
                        "cargando": False,
                    }
                )
        chosen = next((e for e in states if not _screen_recoverable(e)), states[-1])
        results.append(
            {
                "ruta": route,
                "destino": chosen.get("destino"),
                "visitas": [e.get("destino") for e in states],
                **{k: v for k, v in chosen.items() if k != "destino"},
            }
        )
    api._record(f"rutas_{fault}", results)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.goto(f"{FRONTEND}/pedidos")
    time.sleep(duration_s)
    ui.capture(f"04-rutas-{fault}")
    return {
        "falla": fault,
        "recorridas": len(results),
        "blancas": sum(bool(r.get("blanco")) for r in results),
        "errores_prueba": sum("error_prueba" in r for r in results),
    }


def observe_customer_modal(fault: str) -> dict[str, Any]:
    """Envía datos válidos con cada respuesta inyectada y comprueba reintento."""
    page = ui._page()
    clear_rules()
    page.goto(f"{FRONTEND}/clientes")
    page.get_by_role("button", name="Nuevo cliente").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Nombre", exact=True).fill(f"Caos modal {fault}")
    # El alta exige el consentimiento del cliente (Ley N.º 29733): sin él no hay envío.
    dialog.get_by_role("checkbox", name=re.compile("29733")).check()
    set_rules(fault, "POST", r"/api/v1/customers$")
    dialog.get_by_role("button", name="Guardar").click()
    page.wait_for_timeout(17_000 if fault == "timeout" else 1500)
    state = ui.screen_state()
    result = {
        "falla": fault,
        "abierto": dialog.is_visible(),
        "error": dialog.locator('[role="alert"]').all_inner_texts(),
        "guardar_habilitado": dialog.get_by_role("button", name="Guardar").is_enabled(),
        **state,
    }
    ui.capture(f"04-modal-cliente-{fault}")
    api._record(f"modal_cliente_{fault}", result)
    clear_rules()
    return result


def full_matrix(source: str | None = None) -> dict[str, Any]:
    data = json.loads(Path(source).read_text()) if source else api.observations()
    inventory = json.loads(INVENTORY.read_text())
    modals = [e for e in inventory["elementos"] if e["tipo"] == "dialogo"]
    results = []
    for fault in ("500", "409", "503", "timeout"):
        for row in data.get(f"rutas_{fault}", []):
            explicit = bool(row.get("alertas") or row.get("avisos"))
            approved = _screen_recoverable(row) and explicit
            results.append(
                {
                    "elemento": f"ruta:{row['ruta']}",
                    "falla": fault,
                    "estado": "error recuperable" if approved else "no verificado",
                    "alertas": row.get("alertas"),
                    "reintentar": row.get("reintentar"),
                    "cargando": row.get("cargando"),
                    "detalle": row.get("texto", row.get("error_prueba", ""))[:220],
                }
            )
    for modal in modals:
        for fault in ("500", "409", "503", "timeout"):
            by_dialog = data.get(f"dialogos_{fault}") or {}
            observed = by_dialog.get(modal["id"]) or (
                data.get(f"modal_cliente_{fault}")
                if modal["id"] == "dialogo:customers/CustomerDialog"
                else None
            )
            approved = (
                observed
                and observed["abierto"]
                and observed["error"]
                and observed["guardar_habilitado"]
            )
            results.append(
                {
                    "elemento": modal["id"],
                    "falla": fault,
                    "estado": "error recuperable" if approved else "pendiente",
                    "detalle": str(observed["error"])
                    if observed
                    else "No se ejecutó un envío válido de este modal durante la falla.",
                }
            )
    file_path = RUN / "04-matriz.json"
    file_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    approved_count = sum(r["estado"] == "error recuperable" for r in results)
    summary = {
        "total": len(results),
        "aprobadas": approved_count,
        "porcentaje": round(100 * approved_count / len(results), 1),
        "ruta_matriz": str(file_path),
    }
    api._record("matriz", summary)
    return summary


def matrix_100() -> bool:
    result = api.observations().get("matriz")
    return result is None or result["porcentaje"] == 100


def measure_fixed_routes(
    routes: list[str] | None = None, faults: list[str] | None = None
) -> dict[str, Any]:
    """Repite las cuatro inyecciones en las cinco lecturas reparadas."""
    routes = routes or ["/clientes", "/caja", "/reservas", "/comprobantes", "/mesas"]
    results = []
    for fault in faults or ["500", "409", "503", "timeout"]:
        set_rules(fault=fault)
        for route in routes:
            state = _wait_for_navigation(route, 40 if fault == "timeout" else 3)
            result = {
                "ruta": route,
                "falla": fault,
                "alertas": state["alertas"],
                "reintentar": state["reintentar"],
                "blanco": state["blanco"],
                "cargando": state["cargando"],
            }
            result["cumple"] = (
                bool(result["alertas"])
                and result["reintentar"]
                and not result["blanco"]
                and not result["cargando"]
            )
            results.append(result)
            if route == "/clientes":
                ui.capture(f"04-cliente-corregido-{fault}")
    clear_rules()
    api._record("rutas_corregidas", results)
    return {
        "total": len(results),
        "cumplen": sum(r["cumple"] for r in results),
        "porcentaje": round(100 * sum(r["cumple"] for r in results) / len(results), 1),
    }


def fixed_routes_pass() -> bool:
    data = api.observations().get("rutas_corregidas")
    return data is None or all(row["cumple"] for row in data)


def observe_sse() -> dict[str, Any]:
    page = ui._page()
    t0 = time.monotonic()
    with api._client(email=WAITER) as client:
        dish = api.available_dish(client)
        reference = f"SSE-{int(time.time())}"
        new_order = client.post(
            "/orders",
            json={
                "type": "takeaway",
                "customer_name": reference,
                "client_request_id": reference,
                "items": [{"menu_item_id": dish, "quantity": 1}],
            },
        )
        new_order.raise_for_status()
        client.post(f"/orders/{new_order.json()['id']}/send").raise_for_status()
    api._record("sse_referencia", reference)
    page.wait_for_timeout(1000)
    state = ui.screen_state()
    state["segundos"] = round(time.monotonic() - t0, 1)
    state["referencia_visible"] = reference in page.locator("main").inner_text()
    ui.capture("05-sse-congelado")
    api._record("sse_congelado", state)
    return state


def observe_sse_reconnection() -> dict[str, Any]:
    page = ui._page()
    page.wait_for_timeout(6000)
    state = ui.screen_state()
    state["texto"] = page.locator("body").inner_text()[:600]
    state["referencia_visible"] = (
        api.observations().get("sse_referencia", "INEXISTENTE") in page.locator("main").inner_text()
    )
    ui.capture("05-sse-recuperado")
    api._record("sse_recuperado", state)
    return state


def sse_resynced() -> bool:
    observed = api.observations().get("sse_recuperado")
    return observed is None or (observed["referencia_visible"] and not observed["blanco"])
