"""Proxy HTTP de fallas entre el frontend y el backend.

Toxiproxy trabaja en TCP: corta, demora o congela bytes, pero no sabe qué es
una respuesta HTTP. Este proxy sí: reenvía todo al backend y, según las reglas
que le cargue el experimento, responde en su lugar con un 500, un 409, un 503,
se queda callado hasta que el cliente se rinde (timeout), pierde la respuesta
después de que el backend ya la procesó o congela los canales de avisos (SSE)
abiertos.

Se controla por HTTP en `/__caos/*`, así los experimentos de Chaos Toolkit lo
manejan con probes y acciones de tipo `http` o `python`. Solo para pruebas en
local: no autentica a nadie.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

UPSTREAM = os.environ.get("RESTHUB_CHAOS_UPSTREAM", "http://127.0.0.1:8306")
# Lo que responde cada falla. El 500 y el 503 imitan lo que llega de verdad
# cuando algo se rompe: texto plano, sin `detail`, como el de Starlette o el de
# la plataforma delante del servidor. El 409 imita un conflicto del dominio.
FAULT_RESPONSES: dict[str, tuple[int, str, str]] = {
    "500": (500, "text/plain; charset=utf-8", "Internal Server Error"),
    "503": (503, "text/plain; charset=utf-8", "Service Unavailable"),
    "409": (
        409,
        "application/json",
        '{"detail": "Otro cambio llegó antes. Vuelve a cargar e intenta de nuevo."}',
    ),
}
MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Lo que se quita al reenviar: lo decide cada tramo de la conexión.
HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "content-length", "host", "upgrade"}
)


@dataclass
class Rule:
    """Una falla para las peticiones que calcen con el método y la ruta."""

    fault: str
    method: str = "*"
    route: str = ".*"
    exclude: str | None = None
    times: int | None = None
    wait_s: float = 30.0
    applied: int = 0

    def matches(self, method: str, route: str) -> bool:
        if self.times is not None and self.applied >= self.times:
            return False
        if self.method == "MUTACION":
            if method not in MUTATIONS:
                return False
        elif self.method not in ("*", method):
            return False
        if self.exclude and re.search(self.exclude, route):
            return False
        return re.search(self.route, route) is not None


@dataclass
class State:
    rules: list[Rule] = field(default_factory=list)
    in_flight: dict[int, dict[str, Any]] = field(default_factory=dict)
    log: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))
    sse_frozen: asyncio.Event = field(default_factory=asyncio.Event)
    sse_released: asyncio.Event = field(default_factory=asyncio.Event)
    sse_open: int = 0
    next_id: int = 0


state = State()
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(None, connect=5.0))


def _cors(request: Request) -> dict[str, str]:
    # Sin estas cabeceras el navegador esconde el estado y la pantalla vería un
    # corte de red en vez del 500 o el 409 que se quiere probar.
    origin = request.headers.get("origin")
    if not origin:
        return {}
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-credentials": "true",
        "access-control-expose-headers": "X-Request-ID",
        "vary": "Origin",
    }


def _log_request(request: Request, http_status: int | str, injected: str | None) -> None:
    state.log.append(
        {
            "t": round(time.time(), 3),
            "metodo": request.method,
            "ruta": request.url.path,
            "estado": http_status,
            "inyectada": injected,
        }
    )


def _rule_for(request: Request) -> Rule | None:
    if request.method == "OPTIONS":
        return None
    for rule in state.rules:
        if rule.matches(request.method, request.url.path):
            rule.applied += 1
            return rule
    return None


async def _forward(request: Request) -> httpx.Response:
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
    upstream_request = client.build_request(
        request.method,
        request.url.path,
        params=request.query_params,
        headers=headers,
        content=await request.body(),
    )
    return await client.send(upstream_request, stream=True)


async def _sse_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    state.sse_open += 1
    try:
        async for chunk in response.aiter_raw():
            if state.sse_frozen.is_set():
                # Conexión medio abierta: el socket sigue vivo pero ya no pasa
                # nada, como cuando el celular cambia de red o un NAT la olvida.
                await state.sse_released.wait()
            yield chunk
    finally:
        state.sse_open -= 1


async def proxy(request: Request) -> Response:
    route = request.url.path
    if route.startswith("/__caos"):
        return await control(request)
    rule = _rule_for(request)
    if rule is not None and rule.fault in FAULT_RESPONSES:
        status_code, media_type, body = FAULT_RESPONSES[rule.fault]
        _log_request(request, status_code, rule.fault)
        return Response(
            body, status_code=status_code, media_type=media_type, headers=_cors(request)
        )
    if rule is not None and rule.fault == "timeout":
        _log_request(request, "timeout", "timeout")
        # Ni una cabecera: el cliente espera hasta su propio plazo.
        await asyncio.sleep(rule.wait_s)
        return Response(status_code=504, headers=_cors(request))

    number = state.next_id
    state.next_id += 1
    state.in_flight[number] = {"metodo": request.method, "ruta": route, "desde": time.time()}
    try:
        try:
            response = await _forward(request)
        except httpx.HTTPError as error:
            _log_request(request, 502, None)
            return PlainTextResponse(
                f"Bad Gateway: {type(error).__name__}", status_code=502, headers=_cors(request)
            )
        if rule is not None and rule.fault == "perder_respuesta":
            # El backend ya procesó la petición; la respuesta no llega nunca.
            await response.aread()
            await response.aclose()
            _log_request(request, 502, f"perder_respuesta({response.status_code})")
            return PlainTextResponse("Bad Gateway", status_code=502, headers=_cors(request))
        headers = {k: v for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP}
        _log_request(request, response.status_code, None)
        is_sse = response.headers.get("content-type", "").startswith("text/event-stream")
        body = _sse_stream(response) if is_sse else response.aiter_raw()
        return StreamingResponse(
            body,
            status_code=response.status_code,
            headers=headers,
            background=BackgroundTask(response.aclose),
        )
    finally:
        state.in_flight.pop(number, None)


async def control(request: Request) -> Response:
    route = request.url.path.removeprefix("/__caos")
    if route == "/reglas" and request.method == "POST":
        data = await request.json()
        state.rules = [Rule(**r) for r in data.get("reglas", [])]
        return JSONResponse({"reglas": [asdict(r) for r in state.rules]})
    if route == "/reglas" and request.method == "DELETE":
        state.rules = []
        return JSONResponse({"reglas": []})
    if route == "/sse/congelar" and request.method == "POST":
        state.sse_released.clear()
        state.sse_frozen.set()
        return JSONResponse({"sse_congelado": True, "abiertos": state.sse_open})
    if route == "/sse/liberar" and request.method == "POST":
        state.sse_frozen.clear()
        state.sse_released.set()
        return JSONResponse({"sse_congelado": False, "abiertos": state.sse_open})
    if route == "/registro" and request.method == "DELETE":
        state.log.clear()
        return JSONResponse({"registro": []})
    if route == "/estado":
        return JSONResponse(
            {
                "upstream": UPSTREAM,
                "reglas": [asdict(r) for r in state.rules],
                "en_curso": list(state.in_flight.values()),
                "sse_congelado": state.sse_frozen.is_set(),
                "sse_abiertos": state.sse_open,
                "registro": list(state.log),
            }
        )
    return JSONResponse({"detail": "No existe."}, status_code=404)


app = Starlette(
    routes=[
        Route(
            "/{ruta:path}",
            proxy,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        )
    ]
)
