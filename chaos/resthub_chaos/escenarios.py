"""Escenarios UI observables de caída del proceso, fallas HTTP y corte SSE."""

from __future__ import annotations

import json
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


def observar_rutas(falla: str, duracion_s: float = 2.0) -> dict[str, Any]:
    """Recorre cada ruta inventariada bajo falla GET, conservando los resultados crudos."""
    inventario = json.loads(INVENTARIO.read_text())
    rutas = [e["id"].removeprefix("ruta:") for e in inventario["elementos"] if e["tipo"] == "ruta"]
    pagina = ui._pagina()
    resultados = []
    for ruta in rutas:
        # Un id inexistente ejercita la ruta y el estado 404 sin alterar datos.
        destino = (
            ruta.replace(":orderId", "99999999")
            .replace(":invoiceId", "99999999")
            .replace(":restaurantId", "99999999")
            .replace(":menuItemId", "99999999")
            .replace(":kind", "comanda")
            .replace("*", "inexistente")
        )
        try:
            pagina.goto(f"{FRONTEND}{destino}", wait_until="commit", timeout=12000)
            time.sleep(duracion_s)
            estado = ui.estado_pantalla()
            texto = pagina.locator("body").inner_text()[-350:]
            resultados.append({"ruta": ruta, "destino": destino, **estado, "texto": texto})
        except Exception as error:  # noqa: BLE001 - cada ruta queda en la matriz aunque falle
            resultados.append({"ruta": ruta, "destino": destino, "error_prueba": str(error)[:250]})
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
            aprobado = (
                explicito
                and fila.get("reintentar")
                and not fila.get("blanco")
                and not fila.get("cargando")
            )
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
            observado = (
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
