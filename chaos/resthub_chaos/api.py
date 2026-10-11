"""Probes y acciones de Chaos Toolkit contra el API de RestHub.

Un error «limpio» es el que un cliente puede entender y del que puede
reponerse: un 4xx con su `detail`, o un 503 en JSON con un mensaje. Un 500
genérico (`Internal Server Error` en texto plano) o una traza de Python en el
cuerpo no lo son.
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from resthub_chaos import database
from resthub_chaos.config import BACKEND, OWNER, RUN, SEED_CREDENTIAL

API = f"{BACKEND}/api/v1"
OBSERVATIONS = RUN / "observaciones.json"
_TOKENS: dict[str, str] = {}
_load_state: dict[str, Any] = {}


# --------------------------------------------------------------------------
# Observaciones: lo que ve el método durante la falla, para que la hipótesis
# lo juzgue después (la hipótesis se evalúa antes y después del método).
# --------------------------------------------------------------------------


def _read_observations() -> dict[str, Any]:
    try:
        return json.loads(OBSERVATIONS.read_text())
    except (OSError, ValueError):
        return {}


def _record(key: str, value: Any) -> None:
    data = _read_observations()
    data[key] = value
    OBSERVATIONS.parent.mkdir(parents=True, exist_ok=True)
    OBSERVATIONS.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def clear_observations() -> bool:
    OBSERVATIONS.unlink(missing_ok=True)
    return True


def observations() -> dict[str, Any]:
    return _read_observations()


def archive_observations(destination: str) -> dict[str, Any]:
    """Rollback: guarda lo observado junto al journal y deja limpia la próxima corrida."""
    data = _read_observations()
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    clear_observations()
    (RUN / "backend.pid.antes").unlink(missing_ok=True)
    return data


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------


def token(email: str = OWNER) -> str:
    if email not in _TOKENS:
        response = httpx.post(
            f"{API}/auth/login", json={"email": email, "password": SEED_CREDENTIAL}, timeout=30.0
        )
        response.raise_for_status()
        _TOKENS[email] = response.json()["access_token"]
    return _TOKENS[email]


def _client(timeout: float = 30.0, email: str = OWNER) -> httpx.Client:
    return httpx.Client(
        base_url=API, timeout=timeout, headers={"Authorization": f"Bearer {token(email)}"}
    )


def is_clean_error(status: int, kind: str, body: str) -> bool:
    """Un error que un cliente entiende: JSON con `detail` y sin trazas de Python."""
    if "Traceback" in body or 'File "' in body:
        return False
    if status == 500:
        return False
    if not kind.startswith("application/json"):
        return False
    try:
        return bool(json.loads(body).get("detail"))
    except (ValueError, AttributeError):
        return False


def _make_row(step: str, response: httpx.Response | None, error: Exception | None, t0: float):
    row: dict[str, Any] = {"paso": step, "ms": round((time.perf_counter() - t0) * 1000)}
    if response is not None:
        row["estado"] = response.status_code
        row["tipo"] = response.headers.get("content-type", "")
        row["cuerpo"] = response.text[:300]
        if response.status_code >= 400:
            row["limpio"] = is_clean_error(response.status_code, row["tipo"], response.text)
    else:
        row["estado"] = None
        row["error"] = type(error).__name__ if error else None
    return row


def available_dishes(client: httpx.Client) -> list[int]:
    """Los platos que se pueden pedir ahora, sin opciones que elegir."""
    dishes = [
        int(dish["id"])
        for category in client.get("/menu").json()["categories"]
        for dish in category["items"]
        if dish["is_active"]
        and dish["is_available"]
        and not dish["out_of_stock"]
        and not dish["modifier_groups"]
    ]
    if not dishes:
        message = "No hay un plato disponible en la carta."
        raise RuntimeError(message)
    return dishes


def available_dish(client: httpx.Client) -> int:
    # La carga agota el stock de los platos con receta; se reparte entre todos.
    return random.choice(available_dishes(client))


def prepare_dish() -> int:
    with _client() as client:
        dish = available_dish(client)
    _record("plato_latencia", dish)
    return dish


def ensure_cash_open() -> bool:
    with _client() as client:
        if not client.get("/cash/current").json()["is_open"]:
            client.post("/cash/open", json={"opening_amount": "100.00"}).raise_for_status()
    return True


def order_and_charge_flow(
    client: httpx.Client, dish: int, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Abre un pedido para llevar, lo lleva por cocina y lo cobra en efectivo."""
    rows = rows if rows is not None else []
    order_id: int | None = None
    # El último paso que el servidor confirmó con un 2xx.
    last_confirmed: str | None = None
    crid = uuid.uuid4().hex
    steps: list[tuple[str, str, str, dict[str, Any] | None]] = [
        (
            "abrir",
            "POST",
            "/orders",
            {
                "type": "takeaway",
                "customer_name": "Caos",
                "client_request_id": crid,
                "items": [{"menu_item_id": dish, "quantity": 1}],
            },
        ),
        ("enviar", "POST", "/orders/{id}/send", None),
        ("listo", "POST", "/orders/{id}/ready", None),
        ("servido", "POST", "/orders/{id}/served", None),
        ("cobrar", "POST", "/orders/{id}/charge", {"payment_method": "cash"}),
    ]
    for step, method, route, body in steps:
        t0 = time.perf_counter()
        try:
            response = client.request(method, route.format(id=order_id), json=body)
        except httpx.HTTPError as error:
            rows.append(_make_row(step, None, error, t0))
            return {"ok": False, "pedido": order_id, "crid": crid, "confirmado": last_confirmed}
        rows.append(_make_row(step, response, None, t0))
        if response.status_code >= 400:
            return {"ok": False, "pedido": order_id, "crid": crid, "confirmado": last_confirmed}
        last_confirmed = step
        if step == "abrir":
            order_id = int(response.json()["id"])
    return {"ok": True, "pedido": order_id, "crid": crid, "confirmado": last_confirmed}


def order_and_charge_work(attempts: int = 1) -> bool:
    """Probe de estado estable: un pedido completo, de abrir a cobrar, sale bien."""
    ensure_cash_open()
    with _client() as client:
        dish = available_dish(client)
        for _ in range(attempts):
            if order_and_charge_flow(client, dish)["ok"]:
                return True
            time.sleep(1.0)
    return False


def health() -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        response = httpx.get(f"{API}/health", timeout=10.0)
    except httpx.HTTPError as error:
        return {"estado": None, "error": type(error).__name__}
    return {
        "estado": response.status_code,
        "cuerpo": response.text[:300],
        "ms": round((time.perf_counter() - t0) * 1000),
    }


def health_ok() -> bool:
    data = health()
    if data["estado"] != 200:
        return False
    body = json.loads(data["cuerpo"])
    return body.get("status") == "ok" and body.get("database", "ok") == "ok"


def observe_health(key: str = "salud_durante_falla") -> dict[str, Any]:
    data = health()
    _record(key, data)
    return data


def observe_request(key: str, method: str = "GET", route: str = "/orders/active") -> Any:
    t0 = time.perf_counter()
    with _client(timeout=40.0) as client:
        try:
            response = client.request(method, route)
        except httpx.HTTPError as error:
            row = _make_row(route, None, error, t0)
        else:
            row = _make_row(route, response, None, t0)
    _record(key, row)
    return row


def wait_for_recovery(limit_s: float = 60.0) -> dict[str, Any]:
    """Cuánto tarda el API en volver a atender sin reiniciarse."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        if health_ok():
            try:
                if order_and_charge_work():
                    data = {"recuperado": True, "segundos": round(time.monotonic() - t0, 1)}
                    _record("recuperacion", data)
                    return data
            except httpx.HTTPError:
                pass
        time.sleep(0.5)
    data = {"recuperado": False, "segundos": limit_s}
    _record("recuperacion", data)
    return data


# --------------------------------------------------------------------------
# Carga de fondo: pedidos y cobros mientras cae la base
# --------------------------------------------------------------------------


def _generate(duration_s: float, threads: int) -> None:
    end = time.monotonic() + duration_s
    rows: list[dict[str, Any]] = _load_state["filas"]
    lock = threading.Lock()

    def worker() -> None:
        with _client(timeout=40.0) as client:
            dishes = available_dishes(client)
            while time.monotonic() < end:
                own: list[dict[str, Any]] = []
                result = order_and_charge_flow(client, random.choice(dishes), own)
                if own and own[0].get("estado") == 409:
                    # Se agotó ese plato: se vuelve a mirar la carta.
                    with suppress(httpx.HTTPError, RuntimeError, ValueError):
                        dishes = available_dishes(client)
                with lock:
                    for row in own:
                        row["t"] = round(time.time(), 3)
                    rows.extend(own)
                    _load_state["flujos"].append(result)
                if not result["ok"]:
                    time.sleep(0.2)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()


def start_load(duration_s: float = 20.0, threads: int = 3) -> str:
    """Arranca, en segundo plano, pedidos y cobros continuos durante `duracion_s`."""
    ensure_cash_open()
    _load_state.clear()
    _load_state.update({"filas": [], "flujos": [], "inicio": time.time()})
    thread = threading.Thread(target=_generate, args=(duration_s, threads), daemon=True)
    _load_state["hilo"] = thread
    thread.start()
    return f"carga iniciada: {threads} hilos por {duration_s} s"


def finish_load(file_path: str | None = None) -> dict[str, Any]:
    """Espera la carga y anota el resumen: estados, errores sucios y ejemplos."""
    thread: threading.Thread | None = _load_state.get("hilo")
    if thread is not None:
        thread.join()
    rows: list[dict[str, Any]] = _load_state.get("filas", [])
    states: dict[str, int] = {}
    for row in rows:
        key = str(row.get("estado"))
        states[key] = states.get(key, 0) + 1
    dirty = [f for f in rows if f.get("estado") is not None and f.get("limpio") is False]
    no_response = [f for f in rows if f.get("estado") is None]
    summary = {
        "peticiones": len(rows),
        "flujos": len(_load_state.get("flujos", [])),
        "flujos_completos": sum(1 for f in _load_state.get("flujos", []) if f["ok"]),
        "estados": states,
        "errores_sucios": len(dirty),
        "sin_respuesta": len(no_response),
        "ejemplos_sucios": dirty[:3],
        "ejemplos_limpios": [f for f in rows if f.get("limpio") is True][:3],
    }
    summary["confirmadas_perdidas"] = lost_confirmed(_load_state.get("flujos", []))
    _record("carga", summary)
    if file_path:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    return summary


# El estado al que lleva cada paso, en el orden del ciclo del pedido.
STATE_AFTER_STEP = {
    "abrir": "open",
    "enviar": "in_kitchen",
    "listo": "ready",
    "servido": "served",
    "cobrar": "paid",
}
STATE_ORDER = ["open", "in_kitchen", "ready", "served", "paid"]


def lost_confirmed(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los pasos que el servidor confirmó con un 2xx pero que la base no tiene.

    Que la base vaya por delante de lo confirmado es aceptable (la respuesta se
    perdió y el cliente reintenta sin duplicar); que vaya por detrás es una
    escritura perdida: el mesero vio «enviado» o «cobrado» y no quedó guardado.
    """
    confirmed_flows = [f for f in flows if f.get("confirmado")]
    if not confirmed_flows:
        return []
    rows = database.query_crids([f["crid"] for f in confirmed_flows])
    in_db = {row["client_request_id"]: row["status"] for row in rows}
    lost: list[dict[str, Any]] = []
    for flow in confirmed_flows:
        expected = STATE_AFTER_STEP[flow["confirmado"]]
        actual = in_db.get(flow["crid"])
        if actual is None or STATE_ORDER.index(actual) < STATE_ORDER.index(expected):
            lost.append(
                {"pedido": flow["pedido"], "confirmado": flow["confirmado"], "en_base": actual}
            )
    return lost


# --------------------------------------------------------------------------
# Veredictos que lee la hipótesis de estado estable
# --------------------------------------------------------------------------


def clean_errors_during_fault() -> bool:
    """Sin observaciones (antes del método) es verdadero; después, ningún error sucio."""
    load_summary = _read_observations().get("carga")
    if load_summary is None:
        return True
    return load_summary["errores_sucios"] == 0 and load_summary["sin_respuesta"] == 0


def confirmed_responses_persist() -> bool:
    """Todo lo que el API confirmó con un 2xx durante la falla quedó guardado."""
    data = _read_observations()
    load_summary = data.get("carga") or {"confirmadas_perdidas": []}
    after_crash = data.get("confirmada_tras_caida") or {"perdida": False}
    return not load_summary["confirmadas_perdidas"] and not after_crash["perdida"]


def health_reflects_fault() -> bool:
    fault_health = _read_observations().get("salud_durante_falla")
    if fault_health is None:
        return True
    return fault_health.get("estado") == 503


def recovered_on_its_own(limit_s: float = 30.0) -> bool:
    recovery = _read_observations().get("recuperacion")
    if recovery is None:
        return True
    return bool(recovery["recuperado"]) and recovery["segundos"] <= limit_s


def response_within(key: str, limit_ms: int) -> bool:
    row = _read_observations().get(key)
    if row is None:
        return True
    status = row.get("estado")
    clean = status is not None and (status < 400 or row.get("limpio") is True)
    return clean and row["ms"] <= limit_ms


def crash_right_after_confirm(latency_ms: int = 800) -> dict[str, Any]:
    """La base cae en el instante en que el API responde 201 a un pedido nuevo.

    Con latencia en el sentido backend → base, el `COMMIT` tarda en llegar a
    PostgreSQL. Si el API respondiera antes de confirmar, el mesero vería el
    pedido creado y la base nunca lo tendría.
    """
    from resthub_chaos import process, toxiproxy

    with _client(timeout=120.0) as client:
        dish = available_dish(client)
        crid = uuid.uuid4().hex
        toxiproxy.add_latency("postgres", latency_ms, 0, direction="upstream")
        t0 = time.perf_counter()
        try:
            response = client.post(
                "/orders",
                json={
                    "type": "takeaway",
                    "customer_name": "Caos confirmado",
                    "client_request_id": crid,
                    "items": [{"menu_item_id": dish, "quantity": 1}],
                },
            )
            row = _make_row("abrir", response, None, t0)
        except httpx.HTTPError as error:
            row = _make_row("abrir", None, error, t0)
        finally:
            process.stop_database_immediately()
            toxiproxy.remove_toxics("postgres")
    process.start_database()
    in_db = database.orders_with_client_id(crid)
    data = {
        "respuesta": row,
        "en_base": len(in_db),
        "perdida": row.get("estado") == 201 and not in_db,
    }
    _record("confirmada_tras_caida", data)
    return data


def observe_order_opening(key: str, timeout_s: float = 60.0) -> dict[str, Any]:
    """Abre un pedido para llevar durante la falla y anota cuánto tardó y qué respondió."""
    with _client(timeout=timeout_s) as client:
        dish = _read_observations().get("plato_latencia") or available_dish(client)
        crid = uuid.uuid4().hex
        t0 = time.perf_counter()
        try:
            response = client.post(
                "/orders",
                json={
                    "type": "takeaway",
                    "customer_name": "Caos latencia",
                    "client_request_id": crid,
                    "items": [{"menu_item_id": dish, "quantity": 1}],
                },
            )
            row = _make_row("abrir", response, None, t0)
        except httpx.HTTPError as error:
            row = _make_row("abrir", None, error, t0)
    row["crid"] = crid
    _record(key, row)
    return row


def health_in_time(key: str, limit_ms: int) -> bool:
    """El sondeo contestó (200 o 503, los dos en JSON) dentro del plazo."""
    data = _read_observations().get(key)
    if data is None:
        return True
    return data.get("estado") in (200, 503) and data["ms"] <= limit_ms
