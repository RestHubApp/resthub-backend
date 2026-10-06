"""Probes de interfaz con Playwright (la librería de Python, API síncrona).

Chaos Toolkit corre las actividades una tras otra en el mismo hilo, así que el
navegador vive en variables del módulo entre una actividad y la siguiente:
`abrir_sesion` lo arranca con la sesión iniciada antes de la falla, las
observaciones miran la pantalla durante la falla y `cerrar` lo apaga (también
en los rollbacks).

Qué cuenta como pantalla sana durante una falla:
- no queda en blanco (el contenido principal tiene texto o un aviso);
- el hilo principal no se congela (una tarea del navegador vuelve en < 1 s);
- mientras espera, muestra que está cargando (`aria-busy` o «Cargando…»);
- al final muestra los datos o un error legible, con forma de reintentar.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from resthub_chaos import api
from resthub_chaos.config import CLAVE, EVIDENCIAS, FRONTEND, MESERO

LIBS = os.environ.get(
    "RESTHUB_CHAOS_LIBS",
    "/tmp/claude-1000/-home-jeffryru-github/e9544b3f-8ddd-4473-a86c-9413cf96c1be/"
    "scratchpad/libs/root/usr/lib/x86_64-linux-gnu",
)

_ESTADO_JS = """
() => {
  if (!document.body) {
    return { url: location.pathname, largo_main: 0, cargando: true, alertas: [], avisos: [],
             reintentar: false, tareas_largas_ms: [] };
  }
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const main = document.querySelector('main') || document.getElementById('root') || document.body;
  const textoMain = (main.innerText || '').trim();
  const ocupado = [...document.querySelectorAll('[aria-busy="true"]')].some(visible)
    || /Cargando…|Enviando…|Guardando…/.test(document.body.innerText);
  const alertas = [...document.querySelectorAll('[role="alert"]')]
    .filter(visible).map((e) => e.innerText.trim()).filter(Boolean);
  const avisos = [...document.querySelectorAll('[aria-label="Avisos del sistema"] > *')]
    .map((e) => e.innerText.trim()).filter(Boolean);
  const reintentar = [...document.querySelectorAll('button, a')].some(
    (b) => visible(b) && /Reintentar|Volver a intentar|Intentar de nuevo/i.test(b.innerText));
  return {
    url: location.pathname,
    largo_main: textoMain.length,
    cargando: ocupado,
    alertas,
    avisos,
    reintentar,
    tareas_largas_ms: (window.__tareasLargas || []).slice(-5),
  };
}
"""

_OBSERVADOR_JS = """
window.__tareasLargas = [];
try {
  new PerformanceObserver((lista) => {
    for (const e of lista.getEntries()) window.__tareasLargas.push(Math.round(e.duration));
  }).observe({ type: 'longtask', buffered: true });
} catch (e) {}
"""

_nav: dict[str, Any] = {}


def _pagina():
    return _nav["pagina"]


def abrir_navegador(movil: bool = True) -> str:
    """Arranca Chromium con una pantalla de celular (el mesero) o de laptop."""
    from playwright.sync_api import sync_playwright

    os.environ["LD_LIBRARY_PATH"] = f"{LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}"
    cerrar()
    pw = sync_playwright().start()
    navegador = pw.chromium.launch()
    tamano = {"width": 412, "height": 860} if movil else {"width": 1366, "height": 860}
    contexto = navegador.new_context(viewport=tamano, locale="es-PE", timezone_id="America/Lima")
    contexto.add_init_script(_OBSERVADOR_JS)
    pagina = contexto.new_page()
    _nav.update({"pw": pw, "navegador": navegador, "contexto": contexto, "pagina": pagina})
    return "navegador abierto"


def abrir_sesion(correo: str = MESERO, ruta: str = "/pedidos", movil: bool = True) -> str:
    abrir_navegador(movil=movil)
    pagina = _pagina()
    pagina.goto(f"{FRONTEND}/acceso")
    pagina.get_by_label("Correo").fill(correo)
    pagina.get_by_label("Contraseña", exact=True).fill(CLAVE)
    pagina.get_by_role("button", name="Entrar").click()
    pagina.wait_for_url(lambda url: "/acceso" not in url, timeout=30_000)
    pagina.goto(f"{FRONTEND}{ruta}")
    esperar_sin_carga()
    return pagina.url


def esperar_sin_carga(limite_ms: int = 30_000) -> None:
    # No sirve `networkidle`: el canal de avisos (SSE) queda abierto siempre.
    _pagina().wait_for_function(
        "() => document.querySelector('main') && !document.querySelector('[aria-busy=\"true\"]')"
        " && !/Cargando…/.test(document.body.innerText)",
        timeout=limite_ms,
    )


def cerrar() -> str:
    for clave in ("contexto", "navegador"):
        objeto = _nav.pop(clave, None)
        if objeto is not None:
            try:
                objeto.close()
            except Exception:  # noqa: BLE001 - un rollback no puede fallar por esto
                pass
    pw = _nav.pop("pw", None)
    if pw is not None:
        pw.stop()
    _nav.clear()
    return "navegador cerrado"


def estado_pantalla() -> dict[str, Any]:
    pagina = _pagina()
    t0 = time.perf_counter()
    estado: dict[str, Any] = pagina.evaluate(_ESTADO_JS)
    # Lo que tarda el navegador en volver de una tarea: si el hilo principal
    # está bloqueado, `evaluate` no vuelve hasta que se libere.
    pagina.evaluate("() => new Promise((r) => setTimeout(r, 0))")
    estado["hilo_ms"] = round((time.perf_counter() - t0) * 1000)
    estado["blanco"] = estado["largo_main"] < 5 and not estado["alertas"]
    return estado


def capturar(nombre: str, pagina_completa: bool = False) -> str:
    EVIDENCIAS.mkdir(parents=True, exist_ok=True)
    destino = EVIDENCIAS / f"{nombre}.png"
    _pagina().screenshot(path=str(destino), full_page=pagina_completa)
    return str(destino)


def _final(estado: dict[str, Any]) -> str:
    if estado["blanco"]:
        return "blanco"
    if estado["alertas"] or (estado["avisos"] and not estado["cargando"]):
        return "error" if estado["alertas"] or estado["reintentar"] else "aviso"
    if estado["cargando"]:
        return "cargando"
    return "datos"


def observar_bajo_falla(
    clave: str,
    ruta: str = "/pedidos",
    limite_s: float = 45.0,
    capturas_en: tuple[float, ...] = (2.0, 10.0),
    prefijo: str = "ui",
    recargar: bool = True,
    hasta_datos: bool = False,
) -> dict[str, Any]:
    """Abre `ruta` durante la falla y anota cada segundo cómo se ve la pantalla.

    Con `hasta_datos`, un error no corta la observación: tras «Reintentar» el
    aviso anterior sigue a la vista mientras sale la nueva petición, y cortar
    ahí daba por fallida una recuperación que llegaba un momento después.
    """
    pagina = _pagina()
    if recargar:
        pagina.goto(f"{FRONTEND}{ruta}", wait_until="commit")
        # Tras commit React todavía no montó `main`; el documento HTML recién
        # llegado no es una pantalla blanca persistente de la aplicación.
        pagina.locator("main").wait_for(state="attached", timeout=10000)
    t0 = time.monotonic()
    linea: list[dict[str, Any]] = []
    capturas: list[str] = []
    pendientes = list(capturas_en)
    final = "cargando"
    while time.monotonic() - t0 < limite_s:
        transcurrido = round(time.monotonic() - t0, 1)
        estado = estado_pantalla()
        estado["t"] = transcurrido
        linea.append(estado)
        if pendientes and transcurrido >= pendientes[0]:
            capturas.append(capturar(f"{prefijo}-{int(pendientes.pop(0))}s"))
        final = _final(estado)
        terminales = ("datos",) if hasta_datos else ("error", "datos")
        if final in terminales and transcurrido >= (capturas_en[-1] if capturas_en else 0):
            break
        time.sleep(1.0)
    capturas.append(capturar(f"{prefijo}-final"))
    datos = {
        "ruta": ruta,
        "final": final,
        "segundos_hasta_final": linea[-1]["t"] if linea else None,
        "vio_carga": any(e["cargando"] for e in linea[:5]),
        "hubo_blanco": any(e["blanco"] for e in linea),
        "hilo_max_ms": max((e["hilo_ms"] for e in linea), default=0),
        "texto_error": (linea[-1]["alertas"] or linea[-1]["avisos"] or [""])[0] if linea else "",
        "reintentar": bool(linea and linea[-1]["reintentar"]),
        "linea": linea,
        "capturas": capturas,
    }
    api._anotar(clave, datos)
    return {k: v for k, v in datos.items() if k != "linea"}


def reintentar_y_observar(clave: str, prefijo: str = "ui-recuperada", limite_s: float = 30.0):
    """Sin la falla: pulsa «Reintentar» si está a la vista y espera los datos."""
    pagina = _pagina()
    boton = pagina.get_by_role("button", name="Reintentar")
    if boton.count() > 0 and boton.first.is_visible():
        boton.first.click()
    return observar_bajo_falla(
        clave, limite_s=limite_s, capturas_en=(), prefijo=prefijo, recargar=False, hasta_datos=True
    )


# --------------------------------------------------------------------------
# Veredictos para la hipótesis
# --------------------------------------------------------------------------


def pantalla_sana_durante_falla(clave: str, limite_final_s: float = 45.0) -> bool:
    datos = api.observaciones().get(clave)
    if datos is None:
        return True
    return (
        not datos["hubo_blanco"]
        and datos["hilo_max_ms"] < 1000
        and datos["vio_carga"]
        and datos["final"] in ("error", "datos")
        and (datos["segundos_hasta_final"] or 0) <= limite_final_s
    )


def pantalla_recuperada(clave: str) -> bool:
    datos = api.observaciones().get(clave)
    if datos is None:
        return True
    return datos["final"] == "datos" and not datos["hubo_blanco"]


def ruta_de_evidencias() -> str:
    return str(Path(EVIDENCIAS))
