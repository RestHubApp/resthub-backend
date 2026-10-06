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

from resthub_chaos import api, base, ui
from resthub_chaos.config import CLAVE, EVIDENCIAS, FALLAS, FRONTEND, MESERO, RUN

INVENTARIO = Path.home() / "github/wt-pruebas-e2e-front/e2e/cobertura-ui.json"


def reglas(
    falla: str = "500", metodo: str = "GET", ruta: str = "/api/v1/", veces: int | None = None
) -> dict:
    response = httpx.post(
        f"{FALLAS}/__caos/reglas",
        json={
            "reglas": [
                {"falla": falla, "metodo": metodo, "ruta": ruta, "veces": veces, "espera_s": 25}
            ]
        },
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


def limpiar() -> dict:
    return httpx.delete(f"{FALLAS}/__caos/reglas", timeout=5).json()


def liberar_sse() -> dict:
    return httpx.post(f"{FALLAS}/__caos/sse/liberar", timeout=5).json()


def congelar_sse() -> dict:
    return httpx.post(f"{FALLAS}/__caos/sse/congelar", timeout=5).json()


def _esperar_navegacion(ruta: str, segundos: float = 8) -> dict[str, Any]:
    pagina = ui._pagina()
    pagina.goto(f"{FRONTEND}{ruta}", wait_until="commit")
    time.sleep(segundos)
    return ui.estado_pantalla()


def abrir_plataforma() -> str:
    pagina = ui._pagina()
    pagina.goto(f"{FRONTEND}/plataforma/acceso")
    pagina.get_by_label("Correo").fill("plataforma@resthub.dev")
    pagina.get_by_label("Contraseña", exact=True).fill(CLAVE)
    pagina.get_by_role("button", name="Entrar").click()
    pagina.wait_for_url(lambda url: urlparse(url).path == "/plataforma", timeout=20000)
    return pagina.url


def preparar_pedido() -> dict[str, Any]:
    ui.abrir_sesion(MESERO, "/pedidos/nuevo?tipo=llevar&cliente=Caos")
    pagina = ui._pagina()
    pagina.get_by_role("button", name="Agregar", exact=False).first.click(timeout=10000)
    boton = pagina.get_by_role("button", name="Enviar a cocina")
    if boton.count() == 0:
        pagina.get_by_role("button", name="Ver pedido", exact=False).first.click()
    return {"url": pagina.url, "texto": pagina.locator("main").inner_text()[:350]}


def enviar_con_backend_caido() -> dict[str, Any]:
    pagina = ui._pagina()
    pagina.get_by_role("button", name="Enviar a cocina").first.click()
    pagina.wait_for_timeout(18000)
    observacion = ui.estado_pantalla()
    observacion["texto"] = pagina.locator("body").inner_text()[-700:]
    observacion["cola"] = pagina.evaluate(
        "() => localStorage.getItem('resthub.pedidos-sin-enviar.v2')"
    )
    ui.capturar("03-backend-caido")
    api._anotar("pedido_sin_backend", observacion)
    return observacion


def reintentar_tras_arranque() -> dict[str, Any]:
    pagina = ui._pagina()
    pagina.goto(f"{FRONTEND}/pedidos")
    pagina.wait_for_timeout(2000)
    boton = pagina.get_by_role("button", name="Reintentar", exact=False)
    if boton.count():
        boton.first.click()
    pagina.wait_for_timeout(5000)
    ui.capturar("03-pedido-recuperado")
    previo = api.observaciones().get("pedido_sin_backend", {})
    cola = json.loads(previo.get("cola") or "{}")
    # La estructura de la cola puede variar; registrar el contenido crudo y
    # consultar cada id de cliente por separado, nunca inferir duplicados de UI.
    crids: list[str] = []

    def buscar(valor: Any) -> None:
        if isinstance(valor, dict):
            crids.extend(str(v) for k, v in valor.items() if k == "client_request_id")
            for v in valor.values():
                buscar(v)
        elif isinstance(valor, list):
            for v in valor:
                buscar(v)

    buscar(cola)
    reenvios = []
    for entrada in cola if isinstance(cola, list) else []:
        cuerpo = entrada.get("request", {})
        if cuerpo.get("client_request_id"):
            respuesta = httpx.post(
                f"{api.API}/orders",
                json=cuerpo,
                headers={"Authorization": f"Bearer {api.token(MESERO)}"},
                timeout=15,
            )
            reenvios.append({"estado": respuesta.status_code, "pedido": respuesta.json().get("id")})
    with ThreadPoolExecutor(max_workers=1) as pool:
        coincidencias = {c: pool.submit(base.pedidos_con_id_de_cliente, c).result() for c in crids}
    resultado = {
        "crids": crids,
        "reenvios": reenvios,
        "coincidencias": coincidencias,
        "pantalla": ui.estado_pantalla(),
    }
    api._anotar("pedido_recuperado", resultado)
    return resultado


def pedido_exacto() -> bool:
    dato = api.observaciones().get("pedido_recuperado")
    return (
        dato is None
        or bool(dato["crids"])
        and all(len(v) == 1 for v in dato["coincidencias"].values())
    )


def _pantalla_recuperable(estado: dict[str, Any]) -> bool:
    return bool(
        (estado.get("alertas") or estado.get("avisos"))
        and estado.get("reintentar")
        and not estado.get("blanco")
        and not estado.get("cargando")
        and "error_prueba" not in estado
    )


def _ids_reales() -> dict[str, str]:
    """Identificadores que ya existen, leídos del backend directo (sin el proxy de fallas)."""
    from resthub_chaos.dialogos import asegurar_datos

    datos = asegurar_datos()
    return {
        ":orderId": str(datos["order_id"]),
        ":invoiceId": str(datos["invoice_id"]),
        ":restaurantId": str(datos["restaurant_id"]),
        ":menuItemId": str(datos["menu_item_id"]),
    }


def _destinos(ruta: str, ids: dict[str, str]) -> list[str]:
    comun = ruta.replace(":kind", "comanda").replace("*", "inexistente")
    falso = comun
    for token in (":orderId", ":invoiceId", ":restaurantId", ":menuItemId"):
        falso = falso.replace(token, "99999999")
    visitas = [falso]
    if not any(token in ruta for token in ids):
        return visitas
    real = comun
    for token, valor in ids.items():
        real = real.replace(token, valor)
    if real != falso:
        visitas.append(real)
    return visitas


def _esperar_pantalla(segundos: float) -> dict[str, Any]:
    """Espera a que los reintentos terminen y quede un error con forma de reintentar."""
    limite = time.monotonic() + segundos
    estado = ui.estado_pantalla()
    while time.monotonic() < limite:
        estado = ui.estado_pantalla()
        if (
            (estado.get("alertas") or estado.get("avisos"))
            and estado.get("reintentar")
            and not estado.get("blanco")
            and not estado.get("cargando")
        ):
            return estado
        time.sleep(0.5)
    return estado


def observar_rutas(falla: str, duracion_s: float = 2.0) -> dict[str, Any]:
    """Recorre cada ruta con un id inexistente y, si es parametrizada, también con uno real."""
    inventario = json.loads(INVENTARIO.read_text())
    rutas = [e["id"].removeprefix("ruta:") for e in inventario["elementos"] if e["tipo"] == "ruta"]
    ids = _ids_reales()
    pagina = ui._pagina()
    # El 409 no se reintenta; el 500 y el 503 sí, una vez. El timeout agota dos plazos de 15 s.
    espera = duracion_s if duracion_s > 20 else (45.0 if falla == "timeout" else 12.0)
    resultados = []
    for ruta in rutas:
        visitas = _destinos(ruta, ids)
        estados = []
        for destino in visitas:
            try:
                pagina.goto(f"{FRONTEND}{destino}", wait_until="commit", timeout=12000)
                estado = _esperar_pantalla(espera)
                texto = pagina.locator("body").inner_text()[-350:]
                estados.append({"destino": destino, **estado, "texto": texto})
            except Exception as error:  # noqa: BLE001 - cada ruta queda en la matriz aunque falle
                estados.append(
                    {
                        "destino": destino,
                        "error_prueba": str(error)[:250],
                        "alertas": [],
                        "reintentar": False,
                        "blanco": True,
                        "cargando": False,
                    }
                )
        elegido = next((e for e in estados if not _pantalla_recuperable(e)), estados[-1])
        resultados.append(
            {
                "ruta": ruta,
                "destino": elegido.get("destino"),
                "visitas": [e.get("destino") for e in estados],
                **{k: v for k, v in elegido.items() if k != "destino"},
            }
        )
    api._anotar(f"rutas_{falla}", resultados)
    EVIDENCIAS.mkdir(parents=True, exist_ok=True)
    pagina.goto(f"{FRONTEND}/pedidos")
    time.sleep(duracion_s)
    ui.capturar(f"04-rutas-{falla}")
    return {
        "falla": falla,
        "recorridas": len(resultados),
        "blancas": sum(bool(r.get("blanco")) for r in resultados),
        "errores_prueba": sum("error_prueba" in r for r in resultados),
    }


def observar_modal_cliente(falla: str) -> dict[str, Any]:
    """Envía datos válidos con cada respuesta inyectada y comprueba reintento."""
    pagina = ui._pagina()
    limpiar()
    pagina.goto(f"{FRONTEND}/clientes")
    pagina.get_by_role("button", name="Nuevo cliente").click()
    dialogo = pagina.get_by_role("dialog")
    dialogo.get_by_label("Nombre", exact=True).fill(f"Caos modal {falla}")
    # El alta exige el consentimiento del cliente (Ley N.º 29733): sin él no hay envío.
    dialogo.get_by_role("checkbox", name=re.compile("29733")).check()
    reglas(falla, "POST", r"/api/v1/customers$")
    dialogo.get_by_role("button", name="Guardar").click()
    pagina.wait_for_timeout(17_000 if falla == "timeout" else 1500)
    estado = ui.estado_pantalla()
    resultado = {
        "falla": falla,
        "abierto": dialogo.is_visible(),
        "error": dialogo.locator('[role="alert"]').all_inner_texts(),
        "guardar_habilitado": dialogo.get_by_role("button", name="Guardar").is_enabled(),
        **estado,
    }
    ui.capturar(f"04-modal-cliente-{falla}")
    api._anotar(f"modal_cliente_{falla}", resultado)
    limpiar()
    return resultado


def matriz_completa(origen: str | None = None) -> dict[str, Any]:
    datos = json.loads(Path(origen).read_text()) if origen else api.observaciones()
    inventario = json.loads(INVENTARIO.read_text())
    modales = [e for e in inventario["elementos"] if e["tipo"] == "dialogo"]
    resultados = []
    for falla in ("500", "409", "503", "timeout"):
        for fila in datos.get(f"rutas_{falla}", []):
            explicito = bool(fila.get("alertas") or fila.get("avisos"))
            aprobado = _pantalla_recuperable(fila) and explicito
            resultados.append(
                {
                    "elemento": f"ruta:{fila['ruta']}",
                    "falla": falla,
                    "estado": "error recuperable" if aprobado else "no verificado",
                    "alertas": fila.get("alertas"),
                    "reintentar": fila.get("reintentar"),
                    "cargando": fila.get("cargando"),
                    "detalle": fila.get("texto", fila.get("error_prueba", ""))[:220],
                }
            )
    for modal in modales:
        for falla in ("500", "409", "503", "timeout"):
            por_dialogo = datos.get(f"dialogos_{falla}") or {}
            observado = por_dialogo.get(modal["id"]) or (
                datos.get(f"modal_cliente_{falla}")
                if modal["id"] == "dialogo:customers/CustomerDialog"
                else None
            )
            aprobado = (
                observado
                and observado["abierto"]
                and observado["error"]
                and observado["guardar_habilitado"]
            )
            resultados.append(
                {
                    "elemento": modal["id"],
                    "falla": falla,
                    "estado": "error recuperable" if aprobado else "pendiente",
                    "detalle": str(observado["error"])
                    if observado
                    else "No se ejecutó un envío válido de este modal durante la falla.",
                }
            )
    archivo = RUN / "04-matriz.json"
    archivo.write_text(json.dumps(resultados, ensure_ascii=False, indent=2))
    aprobadas = sum(r["estado"] == "error recuperable" for r in resultados)
    resumen = {
        "total": len(resultados),
        "aprobadas": aprobadas,
        "porcentaje": round(100 * aprobadas / len(resultados), 1),
        "ruta_matriz": str(archivo),
    }
    api._anotar("matriz", resumen)
    return resumen


def matriz_100() -> bool:
    resultado = api.observaciones().get("matriz")
    return resultado is None or resultado["porcentaje"] == 100


def medir_rutas_corregidas(
    rutas: list[str] | None = None, fallas: list[str] | None = None
) -> dict[str, Any]:
    """Repite las cuatro inyecciones en las cinco lecturas reparadas."""
    rutas = rutas or ["/clientes", "/caja", "/reservas", "/comprobantes", "/mesas"]
    resultados = []
    for falla in fallas or ["500", "409", "503", "timeout"]:
        reglas(falla=falla)
        for ruta in rutas:
            estado = _esperar_navegacion(ruta, 40 if falla == "timeout" else 3)
            resultado = {
                "ruta": ruta,
                "falla": falla,
                "alertas": estado["alertas"],
                "reintentar": estado["reintentar"],
                "blanco": estado["blanco"],
                "cargando": estado["cargando"],
            }
            resultado["cumple"] = (
                bool(resultado["alertas"])
                and resultado["reintentar"]
                and not resultado["blanco"]
                and not resultado["cargando"]
            )
            resultados.append(resultado)
            if ruta == "/clientes":
                ui.capturar(f"04-cliente-corregido-{falla}")
    limpiar()
    api._anotar("rutas_corregidas", resultados)
    return {
        "total": len(resultados),
        "cumplen": sum(r["cumple"] for r in resultados),
        "porcentaje": round(100 * sum(r["cumple"] for r in resultados) / len(resultados), 1),
    }


def rutas_corregidas_cumplen() -> bool:
    datos = api.observaciones().get("rutas_corregidas")
    return datos is None or all(fila["cumple"] for fila in datos)


def observar_sse() -> dict[str, Any]:
    pagina = ui._pagina()
    t0 = time.monotonic()
    with api._cliente(correo=MESERO) as cliente:
        plato = api.plato_disponible(cliente)
        referencia = f"SSE-{int(time.time())}"
        nuevo = cliente.post(
            "/orders",
            json={
                "type": "takeaway",
                "customer_name": referencia,
                "client_request_id": referencia,
                "items": [{"menu_item_id": plato, "quantity": 1}],
            },
        )
        nuevo.raise_for_status()
        cliente.post(f"/orders/{nuevo.json()['id']}/send").raise_for_status()
    api._anotar("sse_referencia", referencia)
    pagina.wait_for_timeout(1000)
    estado = ui.estado_pantalla()
    estado["segundos"] = round(time.monotonic() - t0, 1)
    estado["referencia_visible"] = referencia in pagina.locator("main").inner_text()
    ui.capturar("05-sse-congelado")
    api._anotar("sse_congelado", estado)
    return estado


def observar_reconexion_sse() -> dict[str, Any]:
    pagina = ui._pagina()
    pagina.wait_for_timeout(6000)
    estado = ui.estado_pantalla()
    estado["texto"] = pagina.locator("body").inner_text()[:600]
    estado["referencia_visible"] = (
        api.observaciones().get("sse_referencia", "INEXISTENTE")
        in pagina.locator("main").inner_text()
    )
    ui.capturar("05-sse-recuperado")
    api._anotar("sse_recuperado", estado)
    return estado


def sse_resincronizado() -> bool:
    observado = api.observaciones().get("sse_recuperado")
    return observado is None or observado["referencia_visible"] and not observado["blanco"]
