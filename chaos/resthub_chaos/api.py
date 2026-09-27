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

from resthub_chaos import base
from resthub_chaos.config import BACKEND, CLAVE, ENCARGADO, RUN

API = f"{BACKEND}/api/v1"
OBSERVACIONES = RUN / "observaciones.json"
_TOKENS: dict[str, str] = {}
_carga: dict[str, Any] = {}


# --------------------------------------------------------------------------
# Observaciones: lo que ve el método durante la falla, para que la hipótesis
# lo juzgue después (la hipótesis se evalúa antes y después del método).
# --------------------------------------------------------------------------


def _leer_observaciones() -> dict[str, Any]:
    try:
        return json.loads(OBSERVACIONES.read_text())
    except (OSError, ValueError):
        return {}


def _anotar(clave: str, valor: Any) -> None:
    datos = _leer_observaciones()
    datos[clave] = valor
    OBSERVACIONES.parent.mkdir(parents=True, exist_ok=True)
    OBSERVACIONES.write_text(json.dumps(datos, ensure_ascii=False, indent=2, default=str))


def borrar_observaciones() -> bool:
    OBSERVACIONES.unlink(missing_ok=True)
    return True


def observaciones() -> dict[str, Any]:
    return _leer_observaciones()


def archivar_observaciones(destino: str) -> dict[str, Any]:
    """Rollback: guarda lo observado junto al journal y deja limpia la próxima corrida."""
    datos = _leer_observaciones()
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    Path(destino).write_text(json.dumps(datos, ensure_ascii=False, indent=2, default=str))
    borrar_observaciones()
    (RUN / "backend.pid.antes").unlink(missing_ok=True)
    return datos


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------


def token(correo: str = ENCARGADO) -> str:
    if correo not in _TOKENS:
        respuesta = httpx.post(
            f"{API}/auth/login", json={"email": correo, "password": CLAVE}, timeout=30.0
        )
        respuesta.raise_for_status()
        _TOKENS[correo] = respuesta.json()["access_token"]
    return _TOKENS[correo]


def _cliente(timeout: float = 30.0, correo: str = ENCARGADO) -> httpx.Client:
    return httpx.Client(
        base_url=API, timeout=timeout, headers={"Authorization": f"Bearer {token(correo)}"}
    )


def es_error_limpio(estado: int, tipo: str, cuerpo: str) -> bool:
    """Un error que un cliente entiende: JSON con `detail` y sin trazas de Python."""
    if "Traceback" in cuerpo or 'File "' in cuerpo:
        return False
    if estado == 500:
        return False
    if not tipo.startswith("application/json"):
        return False
    try:
        return bool(json.loads(cuerpo).get("detail"))
    except (ValueError, AttributeError):
        return False


def _registro(paso: str, respuesta: httpx.Response | None, error: Exception | None, t0: float):
    fila: dict[str, Any] = {"paso": paso, "ms": round((time.perf_counter() - t0) * 1000)}
    if respuesta is not None:
        fila["estado"] = respuesta.status_code
        fila["tipo"] = respuesta.headers.get("content-type", "")
        fila["cuerpo"] = respuesta.text[:300]
        if respuesta.status_code >= 400:
            fila["limpio"] = es_error_limpio(respuesta.status_code, fila["tipo"], respuesta.text)
    else:
        fila["estado"] = None
        fila["error"] = type(error).__name__ if error else None
    return fila


def platos_disponibles(cliente: httpx.Client) -> list[int]:
    """Los platos que se pueden pedir ahora, sin opciones que elegir."""
    platos = [
        int(plato["id"])
        for categoria in cliente.get("/menu").json()["categories"]
        for plato in categoria["items"]
        if plato["is_active"]
        and plato["is_available"]
        and not plato["out_of_stock"]
        and not plato["modifier_groups"]
    ]
    if not platos:
        raise RuntimeError("No hay un plato disponible en la carta.")
    return platos


def plato_disponible(cliente: httpx.Client) -> int:
    # La carga agota el stock de los platos con receta; se reparte entre todos.
    return random.choice(platos_disponibles(cliente))


def preparar_plato() -> int:
    with _cliente() as cliente:
        plato = plato_disponible(cliente)
    _anotar("plato_latencia", plato)
    return plato


def asegurar_caja_abierta() -> bool:
    with _cliente() as cliente:
        if not cliente.get("/cash/current").json()["is_open"]:
            cliente.post("/cash/open", json={"opening_amount": "100.00"}).raise_for_status()
    return True


def flujo_pedido_y_cobro(
    cliente: httpx.Client, plato: int, filas: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Abre un pedido para llevar, lo lleva por cocina y lo cobra en efectivo."""
    filas = filas if filas is not None else []
    pedido_id: int | None = None
    # El último paso que el servidor confirmó con un 2xx.
    confirmado: str | None = None
    crid = uuid.uuid4().hex
    pasos: list[tuple[str, str, str, dict[str, Any] | None]] = [
        (
            "abrir",
            "POST",
            "/orders",
            {
                "type": "takeaway",
                "customer_name": "Caos",
                "client_request_id": crid,
                "items": [{"menu_item_id": plato, "quantity": 1}],
            },
        ),
        ("enviar", "POST", "/orders/{id}/send", None),
        ("listo", "POST", "/orders/{id}/ready", None),
        ("servido", "POST", "/orders/{id}/served", None),
        ("cobrar", "POST", "/orders/{id}/charge", {"payment_method": "cash"}),
    ]
    for paso, metodo, ruta, cuerpo in pasos:
        t0 = time.perf_counter()
        try:
            respuesta = cliente.request(metodo, ruta.format(id=pedido_id), json=cuerpo)
        except httpx.HTTPError as error:
            filas.append(_registro(paso, None, error, t0))
            return {"ok": False, "pedido": pedido_id, "crid": crid, "confirmado": confirmado}
        filas.append(_registro(paso, respuesta, None, t0))
        if respuesta.status_code >= 400:
            return {"ok": False, "pedido": pedido_id, "crid": crid, "confirmado": confirmado}
        confirmado = paso
        if paso == "abrir":
            pedido_id = int(respuesta.json()["id"])
    return {"ok": True, "pedido": pedido_id, "crid": crid, "confirmado": confirmado}


def pedido_y_cobro_funcionan(intentos: int = 1) -> bool:
    """Probe de estado estable: un pedido completo, de abrir a cobrar, sale bien."""
    asegurar_caja_abierta()
    with _cliente() as cliente:
        plato = plato_disponible(cliente)
        for _ in range(intentos):
            if flujo_pedido_y_cobro(cliente, plato)["ok"]:
                return True
            time.sleep(1.0)
    return False


def salud() -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        respuesta = httpx.get(f"{API}/health", timeout=10.0)
    except httpx.HTTPError as error:
        return {"estado": None, "error": type(error).__name__}
    return {
        "estado": respuesta.status_code,
        "cuerpo": respuesta.text[:300],
        "ms": round((time.perf_counter() - t0) * 1000),
    }


def salud_ok() -> bool:
    datos = salud()
    if datos["estado"] != 200:
        return False
    cuerpo = json.loads(datos["cuerpo"])
    return cuerpo.get("status") == "ok" and cuerpo.get("database", "ok") == "ok"


def observar_salud(clave: str = "salud_durante_falla") -> dict[str, Any]:
    datos = salud()
    _anotar(clave, datos)
    return datos


def observar_peticion(clave: str, metodo: str = "GET", ruta: str = "/orders/active") -> Any:
    t0 = time.perf_counter()
    with _cliente(timeout=40.0) as cliente:
        try:
            respuesta = cliente.request(metodo, ruta)
        except httpx.HTTPError as error:
            fila = _registro(ruta, None, error, t0)
        else:
            fila = _registro(ruta, respuesta, None, t0)
    _anotar(clave, fila)
    return fila


def esperar_recuperacion(limite_s: float = 60.0) -> dict[str, Any]:
    """Cuánto tarda el API en volver a atender sin reiniciarse."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limite_s:
        if salud_ok():
            try:
                if pedido_y_cobro_funcionan():
                    datos = {"recuperado": True, "segundos": round(time.monotonic() - t0, 1)}
                    _anotar("recuperacion", datos)
                    return datos
            except httpx.HTTPError:
                pass
        time.sleep(0.5)
    datos = {"recuperado": False, "segundos": limite_s}
    _anotar("recuperacion", datos)
    return datos


# --------------------------------------------------------------------------
# Carga de fondo: pedidos y cobros mientras cae la base
# --------------------------------------------------------------------------


def _generar(duracion_s: float, hilos: int) -> None:
    fin = time.monotonic() + duracion_s
    filas: list[dict[str, Any]] = _carga["filas"]
    candado = threading.Lock()

    def trabajador() -> None:
        with _cliente(timeout=40.0) as cliente:
            platos = platos_disponibles(cliente)
            while time.monotonic() < fin:
                propias: list[dict[str, Any]] = []
                resultado = flujo_pedido_y_cobro(cliente, random.choice(platos), propias)
                if propias and propias[0].get("estado") == 409:
                    # Se agotó ese plato: se vuelve a mirar la carta.
                    with suppress(httpx.HTTPError, RuntimeError, ValueError):
                        platos = platos_disponibles(cliente)
                with candado:
                    for fila in propias:
                        fila["t"] = round(time.time(), 3)
                    filas.extend(propias)
                    _carga["flujos"].append(resultado)
                if not resultado["ok"]:
                    time.sleep(0.2)

    trabajadores = [threading.Thread(target=trabajador) for _ in range(hilos)]
    for hilo in trabajadores:
        hilo.start()
    for hilo in trabajadores:
        hilo.join()


def iniciar_carga(duracion_s: float = 20.0, hilos: int = 3) -> str:
    """Arranca, en segundo plano, pedidos y cobros continuos durante `duracion_s`."""
    asegurar_caja_abierta()
    _carga.clear()
    _carga.update({"filas": [], "flujos": [], "inicio": time.time()})
    hilo = threading.Thread(target=_generar, args=(duracion_s, hilos), daemon=True)
    _carga["hilo"] = hilo
    hilo.start()
    return f"carga iniciada: {hilos} hilos por {duracion_s} s"


def terminar_carga(archivo: str | None = None) -> dict[str, Any]:
    """Espera la carga y anota el resumen: estados, errores sucios y ejemplos."""
    hilo: threading.Thread | None = _carga.get("hilo")
    if hilo is not None:
        hilo.join()
    filas: list[dict[str, Any]] = _carga.get("filas", [])
    estados: dict[str, int] = {}
    for fila in filas:
        clave = str(fila.get("estado"))
        estados[clave] = estados.get(clave, 0) + 1
    sucios = [f for f in filas if f.get("estado") is not None and f.get("limpio") is False]
    sin_respuesta = [f for f in filas if f.get("estado") is None]
    resumen = {
        "peticiones": len(filas),
        "flujos": len(_carga.get("flujos", [])),
        "flujos_completos": sum(1 for f in _carga.get("flujos", []) if f["ok"]),
        "estados": estados,
        "errores_sucios": len(sucios),
        "sin_respuesta": len(sin_respuesta),
        "ejemplos_sucios": sucios[:3],
        "ejemplos_limpios": [f for f in filas if f.get("limpio") is True][:3],
    }
    resumen["confirmadas_perdidas"] = confirmadas_perdidas(_carga.get("flujos", []))
    _anotar("carga", resumen)
    if archivo:
        Path(archivo).parent.mkdir(parents=True, exist_ok=True)
        Path(archivo).write_text(json.dumps(filas, ensure_ascii=False, indent=1))
    return resumen


# El estado al que lleva cada paso, en el orden del ciclo del pedido.
ESTADO_TRAS_PASO = {
    "abrir": "open",
    "enviar": "in_kitchen",
    "listo": "ready",
    "servido": "served",
    "cobrar": "paid",
}
ORDEN_ESTADOS = ["open", "in_kitchen", "ready", "served", "paid"]


def confirmadas_perdidas(flujos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los pasos que el servidor confirmó con un 2xx pero que la base no tiene.

    Que la base vaya por delante de lo confirmado es aceptable (la respuesta se
    perdió y el cliente reintenta sin duplicar); que vaya por detrás es una
    escritura perdida: el mesero vio «enviado» o «cobrado» y no quedó guardado.
    """
    confirmados = [f for f in flujos if f.get("confirmado")]
    if not confirmados:
        return []
    filas = base.consultar_crid([f["crid"] for f in confirmados])
    en_base = {fila["client_request_id"]: fila["status"] for fila in filas}
    perdidas: list[dict[str, Any]] = []
    for flujo in confirmados:
        esperado = ESTADO_TRAS_PASO[flujo["confirmado"]]
        actual = en_base.get(flujo["crid"])
        if actual is None or ORDEN_ESTADOS.index(actual) < ORDEN_ESTADOS.index(esperado):
            perdidas.append(
                {"pedido": flujo["pedido"], "confirmado": flujo["confirmado"], "en_base": actual}
            )
    return perdidas


# --------------------------------------------------------------------------
# Veredictos que lee la hipótesis de estado estable
# --------------------------------------------------------------------------


def errores_limpios_durante_la_falla() -> bool:
    """Sin observaciones (antes del método) es verdadero; después, ningún error sucio."""
    carga = _leer_observaciones().get("carga")
    if carga is None:
        return True
    return carga["errores_sucios"] == 0 and carga["sin_respuesta"] == 0


def respuestas_confirmadas_persisten() -> bool:
    """Todo lo que el API confirmó con un 2xx durante la falla quedó guardado."""
    datos = _leer_observaciones()
    carga = datos.get("carga") or {"confirmadas_perdidas": []}
    tras_caida = datos.get("confirmada_tras_caida") or {"perdida": False}
    return not carga["confirmadas_perdidas"] and not tras_caida["perdida"]


def salud_reflejo_la_falla() -> bool:
    salud_falla = _leer_observaciones().get("salud_durante_falla")
    if salud_falla is None:
        return True
    return salud_falla.get("estado") == 503


def se_recupero_sola(limite_s: float = 30.0) -> bool:
    recuperacion = _leer_observaciones().get("recuperacion")
    if recuperacion is None:
        return True
    return bool(recuperacion["recuperado"]) and recuperacion["segundos"] <= limite_s


def respuesta_dentro_de(clave: str, limite_ms: int) -> bool:
    fila = _leer_observaciones().get(clave)
    if fila is None:
        return True
    estado = fila.get("estado")
    limpio = estado is not None and (estado < 400 or fila.get("limpio") is True)
    return limpio and fila["ms"] <= limite_ms


def caida_justo_despues_de_confirmar(latencia_ms: int = 800) -> dict[str, Any]:
    """La base cae en el instante en que el API responde 201 a un pedido nuevo.

    Con latencia en el sentido backend → base, el `COMMIT` tarda en llegar a
    PostgreSQL. Si el API respondiera antes de confirmar, el mesero vería el
    pedido creado y la base nunca lo tendría.
    """
    from resthub_chaos import proceso, toxiproxy

    with _cliente(timeout=120.0) as cliente:
        plato = plato_disponible(cliente)
        crid = uuid.uuid4().hex
        toxiproxy.agregar_latencia("postgres", latencia_ms, 0, sentido="upstream")
        t0 = time.perf_counter()
        try:
            respuesta = cliente.post(
                "/orders",
                json={
                    "type": "takeaway",
                    "customer_name": "Caos confirmado",
                    "client_request_id": crid,
                    "items": [{"menu_item_id": plato, "quantity": 1}],
                },
            )
            fila = _registro("abrir", respuesta, None, t0)
        except httpx.HTTPError as error:
            fila = _registro("abrir", None, error, t0)
        finally:
            proceso.detener_base_inmediato()
            toxiproxy.quitar_toxicos("postgres")
    proceso.arrancar_base()
    en_base = base.pedidos_con_id_de_cliente(crid)
    datos = {
        "respuesta": fila,
        "en_base": len(en_base),
        "perdida": fila.get("estado") == 201 and not en_base,
    }
    _anotar("confirmada_tras_caida", datos)
    return datos


def observar_apertura_pedido(clave: str, timeout_s: float = 60.0) -> dict[str, Any]:
    """Abre un pedido para llevar durante la falla y anota cuánto tardó y qué respondió."""
    with _cliente(timeout=timeout_s) as cliente:
        plato = _leer_observaciones().get("plato_latencia") or plato_disponible(cliente)
        crid = uuid.uuid4().hex
        t0 = time.perf_counter()
        try:
            respuesta = cliente.post(
                "/orders",
                json={
                    "type": "takeaway",
                    "customer_name": "Caos latencia",
                    "client_request_id": crid,
                    "items": [{"menu_item_id": plato, "quantity": 1}],
                },
            )
            fila = _registro("abrir", respuesta, None, t0)
        except httpx.HTTPError as error:
            fila = _registro("abrir", None, error, t0)
    fila["crid"] = crid
    _anotar(clave, fila)
    return fila


def salud_a_tiempo(clave: str, limite_ms: int) -> bool:
    """El sondeo contestó (200 o 503, los dos en JSON) dentro del plazo."""
    datos = _leer_observaciones().get(clave)
    if datos is None:
        return True
    return datos.get("estado") in (200, 503) and datos["ms"] <= limite_ms
