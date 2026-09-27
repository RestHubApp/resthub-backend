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
CUERPOS: dict[str, tuple[int, str, str]] = {
    "500": (500, "text/plain; charset=utf-8", "Internal Server Error"),
    "503": (503, "text/plain; charset=utf-8", "Service Unavailable"),
    "409": (
        409,
        "application/json",
        '{"detail": "Otro cambio llegó antes. Vuelve a cargar e intenta de nuevo."}',
    ),
}
MUTACIONES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Lo que se quita al reenviar: lo decide cada tramo de la conexión.
SALTO = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "content-length", "host", "upgrade"}
)


@dataclass
class Regla:
    """Una falla para las peticiones que calcen con el método y la ruta."""

    falla: str
    metodo: str = "*"
    ruta: str = ".*"
    excluir: str | None = None
    veces: int | None = None
    espera_s: float = 30.0
    aplicada: int = 0

    def calza(self, metodo: str, ruta: str) -> bool:
        if self.veces is not None and self.aplicada >= self.veces:
            return False
        if self.metodo == "MUTACION":
            if metodo not in MUTACIONES:
                return False
        elif self.metodo != "*" and self.metodo != metodo:
            return False
        if self.excluir and re.search(self.excluir, ruta):
            return False
        return re.search(self.ruta, ruta) is not None


@dataclass
class Estado:
    reglas: list[Regla] = field(default_factory=list)
    en_curso: dict[int, dict[str, Any]] = field(default_factory=dict)
    registro: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))
    sse_congelado: asyncio.Event = field(default_factory=asyncio.Event)
    sse_abiertos: int = 0
    siguiente: int = 0


estado = Estado()
cliente = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(None, connect=5.0))


def _cors(request: Request) -> dict[str, str]:
    # Sin estas cabeceras el navegador esconde el estado y la pantalla vería un
    # corte de red en vez del 500 o el 409 que se quiere probar.
    origen = request.headers.get("origin")
    if not origen:
        return {}
    return {
        "access-control-allow-origin": origen,
        "access-control-allow-credentials": "true",
        "access-control-expose-headers": "X-Request-ID",
        "vary": "Origin",
    }


def _anotar(request: Request, estado_http: int | str, inyectada: str | None) -> None:
    estado.registro.append(
        {
            "t": round(time.time(), 3),
            "metodo": request.method,
            "ruta": request.url.path,
            "estado": estado_http,
            "inyectada": inyectada,
        }
    )


def _regla_para(request: Request) -> Regla | None:
    if request.method == "OPTIONS":
        return None
    for regla in estado.reglas:
        if regla.calza(request.method, request.url.path):
            regla.aplicada += 1
            return regla
    return None


async def _reenviar(request: Request) -> httpx.Response:
    cabeceras = [(k, v) for k, v in request.headers.items() if k.lower() not in SALTO]
    peticion = cliente.build_request(
        request.method,
        request.url.path,
        params=request.query_params,
        headers=cabeceras,
        content=await request.body(),
    )
    return await cliente.send(peticion, stream=True)


async def _flujo_sse(respuesta: httpx.Response) -> AsyncIterator[bytes]:
    estado.sse_abiertos += 1
    try:
        async for trozo in respuesta.aiter_raw():
            if estado.sse_congelado.is_set():
                # Conexión medio abierta: el socket sigue vivo pero ya no pasa
                # nada, como cuando el celular cambia de red o un NAT la olvida.
                await asyncio.Event().wait()
            yield trozo
    finally:
        estado.sse_abiertos -= 1


async def proxy(request: Request) -> Response:
    ruta = request.url.path
    if ruta.startswith("/__caos"):
        return await control(request)
    regla = _regla_para(request)
    if regla is not None and regla.falla in CUERPOS:
        codigo, tipo, cuerpo = CUERPOS[regla.falla]
        _anotar(request, codigo, regla.falla)
        return Response(cuerpo, status_code=codigo, media_type=tipo, headers=_cors(request))
    if regla is not None and regla.falla == "timeout":
        _anotar(request, "timeout", "timeout")
        # Ni una cabecera: el cliente espera hasta su propio plazo.
        await asyncio.sleep(regla.espera_s)
        return Response(status_code=504, headers=_cors(request))

    numero = estado.siguiente
    estado.siguiente += 1
    estado.en_curso[numero] = {"metodo": request.method, "ruta": ruta, "desde": time.time()}
    try:
        try:
            respuesta = await _reenviar(request)
        except httpx.HTTPError as error:
            _anotar(request, 502, None)
            return PlainTextResponse(
                f"Bad Gateway: {type(error).__name__}", status_code=502, headers=_cors(request)
            )
        if regla is not None and regla.falla == "perder_respuesta":
            # El backend ya procesó la petición; la respuesta no llega nunca.
            await respuesta.aread()
            await respuesta.aclose()
            _anotar(request, 502, f"perder_respuesta({respuesta.status_code})")
            return PlainTextResponse("Bad Gateway", status_code=502, headers=_cors(request))
        cabeceras = {k: v for k, v in respuesta.headers.items() if k.lower() not in SALTO}
        _anotar(request, respuesta.status_code, None)
        es_sse = respuesta.headers.get("content-type", "").startswith("text/event-stream")
        cuerpo = _flujo_sse(respuesta) if es_sse else respuesta.aiter_raw()
        return StreamingResponse(
            cuerpo,
            status_code=respuesta.status_code,
            headers=cabeceras,
            background=BackgroundTask(respuesta.aclose),
        )
    finally:
        estado.en_curso.pop(numero, None)


async def control(request: Request) -> Response:
    ruta = request.url.path.removeprefix("/__caos")
    if ruta == "/reglas" and request.method == "POST":
        datos = await request.json()
        estado.reglas = [Regla(**r) for r in datos.get("reglas", [])]
        return JSONResponse({"reglas": [asdict(r) for r in estado.reglas]})
    if ruta == "/reglas" and request.method == "DELETE":
        estado.reglas = []
        return JSONResponse({"reglas": []})
    if ruta == "/sse/congelar" and request.method == "POST":
        estado.sse_congelado.set()
        return JSONResponse({"sse_congelado": True, "abiertos": estado.sse_abiertos})
    if ruta == "/sse/liberar" and request.method == "POST":
        estado.sse_congelado.clear()
        return JSONResponse({"sse_congelado": False, "abiertos": estado.sse_abiertos})
    if ruta == "/registro" and request.method == "DELETE":
        estado.registro.clear()
        return JSONResponse({"registro": []})
    if ruta == "/estado":
        return JSONResponse(
            {
                "upstream": UPSTREAM,
                "reglas": [asdict(r) for r in estado.reglas],
                "en_curso": list(estado.en_curso.values()),
                "sse_congelado": estado.sse_congelado.is_set(),
                "sse_abiertos": estado.sse_abiertos,
                "registro": list(estado.registro),
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
