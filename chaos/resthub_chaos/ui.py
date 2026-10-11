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
from contextlib import suppress
from pathlib import Path
from typing import Any

from resthub_chaos import api
from resthub_chaos.config import EVIDENCE, FRONTEND, SEED_CREDENTIAL, WAITER

LIBS = os.environ.get(
    "RESTHUB_CHAOS_LIBS",
    "/tmp/claude-1000/-home-jeffryru-github/e9544b3f-8ddd-4473-a86c-9413cf96c1be/"
    "scratchpad/libs/root/usr/lib/x86_64-linux-gnu",
)

_STATE_JS = """
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

_OBSERVER_JS = """
window.__tareasLargas = [];
try {
  new PerformanceObserver((lista) => {
    for (const e of lista.getEntries()) window.__tareasLargas.push(Math.round(e.duration));
  }).observe({ type: 'longtask', buffered: true });
} catch (e) {}
"""

_nav: dict[str, Any] = {}


def _page():
    return _nav["pagina"]


def open_browser(mobile: bool = True) -> str:
    """Arranca Chromium con una pantalla de celular (el mesero) o de laptop."""
    from playwright.sync_api import sync_playwright

    os.environ["LD_LIBRARY_PATH"] = f"{LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}"
    close()
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    size = {"width": 412, "height": 860} if mobile else {"width": 1366, "height": 860}
    context = browser.new_context(viewport=size, locale="es-PE", timezone_id="America/Lima")
    context.add_init_script(_OBSERVER_JS)
    page = context.new_page()
    _nav.update({"pw": pw, "navegador": browser, "contexto": context, "pagina": page})
    return "navegador abierto"


def open_session(email: str = WAITER, route: str = "/pedidos", mobile: bool = True) -> str:
    open_browser(mobile=mobile)
    page = _page()
    page.goto(f"{FRONTEND}/acceso")
    page.get_by_label("Correo").fill(email)
    page.get_by_label("Contraseña", exact=True).fill(SEED_CREDENTIAL)
    page.get_by_role("button", name="Entrar").click()
    page.wait_for_url(lambda url: "/acceso" not in url, timeout=30_000)
    page.goto(f"{FRONTEND}{route}")
    wait_until_loaded()
    return page.url


def wait_until_loaded(limit_ms: int = 30_000) -> None:
    # No sirve `networkidle`: el canal de avisos (SSE) queda abierto siempre.
    _page().wait_for_function(
        "() => document.querySelector('main') && !document.querySelector('[aria-busy=\"true\"]')"
        " && !/Cargando…/.test(document.body.innerText)",
        timeout=limit_ms,
    )


def close() -> str:
    for key in ("contexto", "navegador"):
        obj = _nav.pop(key, None)
        if obj is not None:
            with suppress(Exception):  # un cierre fallido no debe impedir cerrar el resto
                obj.close()
    pw = _nav.pop("pw", None)
    if pw is not None:
        pw.stop()
    _nav.clear()
    return "navegador cerrado"


def screen_state() -> dict[str, Any]:
    page = _page()
    t0 = time.perf_counter()
    state: dict[str, Any] = page.evaluate(_STATE_JS)
    # Lo que tarda el navegador en volver de una tarea: si el hilo principal
    # está bloqueado, `evaluate` no vuelve hasta que se libere.
    page.evaluate("() => new Promise((r) => setTimeout(r, 0))")
    state["hilo_ms"] = round((time.perf_counter() - t0) * 1000)
    state["blanco"] = state["largo_main"] < 5 and not state["alertas"]
    return state


def capture(name: str, full_page: bool = False) -> str:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    destination = EVIDENCE / f"{name}.png"
    _page().screenshot(path=str(destination), full_page=full_page)
    return str(destination)


def _final(state: dict[str, Any]) -> str:
    if state["blanco"]:
        return "blanco"
    if state["alertas"] or (state["avisos"] and not state["cargando"]):
        return "error" if state["alertas"] or state["reintentar"] else "aviso"
    if state["cargando"]:
        return "cargando"
    return "datos"


def observe_under_fault(
    key: str,
    route: str = "/pedidos",
    limit_s: float = 45.0,
    capture_at: tuple[float, ...] = (2.0, 10.0),
    prefix: str = "ui",
    reload: bool = True,
    until_data: bool = False,
) -> dict[str, Any]:
    """Abre `ruta` durante la falla y anota cada segundo cómo se ve la pantalla.

    Con `hasta_datos`, un error no corta la observación: tras «Reintentar» el
    aviso anterior sigue a la vista mientras sale la nueva petición, y cortar
    ahí daba por fallida una recuperación que llegaba un momento después.
    """
    page = _page()
    if reload:
        page.goto(f"{FRONTEND}{route}", wait_until="commit")
        # Tras commit React todavía no montó `main`; el documento HTML recién
        # llegado no es una pantalla blanca persistente de la aplicación.
        page.locator("main").wait_for(state="attached", timeout=10000)
    t0 = time.monotonic()
    timeline: list[dict[str, Any]] = []
    captures: list[str] = []
    pending = list(capture_at)
    final = "cargando"
    while time.monotonic() - t0 < limit_s:
        elapsed = round(time.monotonic() - t0, 1)
        state = screen_state()
        state["t"] = elapsed
        timeline.append(state)
        if pending and elapsed >= pending[0]:
            captures.append(capture(f"{prefix}-{int(pending.pop(0))}s"))
        final = _final(state)
        terminal = ("datos",) if until_data else ("error", "datos")
        if final in terminal and elapsed >= (capture_at[-1] if capture_at else 0):
            break
        time.sleep(1.0)
    captures.append(capture(f"{prefix}-final"))
    data = {
        "ruta": route,
        "final": final,
        "segundos_hasta_final": timeline[-1]["t"] if timeline else None,
        "vio_carga": any(e["cargando"] for e in timeline[:5]),
        "hubo_blanco": any(e["blanco"] for e in timeline),
        "hilo_max_ms": max((e["hilo_ms"] for e in timeline), default=0),
        "texto_error": (timeline[-1]["alertas"] or timeline[-1]["avisos"] or [""])[0]
        if timeline
        else "",
        "reintentar": bool(timeline and timeline[-1]["reintentar"]),
        "linea": timeline,
        "capturas": captures,
    }
    api._record(key, data)
    return {k: v for k, v in data.items() if k != "linea"}


def retry_and_observe(key: str, prefix: str = "ui-recuperada", limit_s: float = 30.0):
    """Sin la falla: pulsa «Reintentar» si está a la vista y espera los datos."""
    page = _page()
    button = page.get_by_role("button", name="Reintentar")
    if button.count() > 0 and button.first.is_visible():
        button.first.click()
    return observe_under_fault(
        key, limit_s=limit_s, capture_at=(), prefix=prefix, reload=False, until_data=True
    )


# --------------------------------------------------------------------------
# Veredictos para la hipótesis
# --------------------------------------------------------------------------


def healthy_screen_during_fault(key: str, final_limit_s: float = 45.0) -> bool:
    data = api.observations().get(key)
    if data is None:
        return True
    return (
        not data["hubo_blanco"]
        and data["hilo_max_ms"] < 1000
        and data["vio_carga"]
        and data["final"] in ("error", "datos")
        and (data["segundos_hasta_final"] or 0) <= final_limit_s
    )


def screen_recovered(key: str) -> bool:
    data = api.observations().get(key)
    if data is None:
        return True
    return data["final"] == "datos" and not data["hubo_blanco"]


def evidence_path() -> str:
    return str(Path(EVIDENCE))
