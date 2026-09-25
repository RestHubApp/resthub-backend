"""Registro de cada petición HTTP.

Es un middleware ASGI puro y no uno de `BaseHTTPMiddleware`: ese corre la
aplicación en otra tarea, y las variables de contexto que se guardan acá no
llegarían a los logs que escriben los casos de uso.

Cada petición recibe un identificador. Si el frontend manda uno válido en
`X-Request-ID` se respeta, así el error que ve la consola del navegador y el
del servidor se encuentran buscando el mismo valor. Vuelve en la respuesta con
la misma cabecera.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from resthub.core.logs import get_logger

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_HEADER_KEY = REQUEST_ID_HEADER.lower().encode("latin-1")
# Un identificador ajeno se acepta solo con esta forma: evita que un cliente
# meta saltos de línea o textos enormes en los logs.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,128}")
# El sondeo de vida se consulta cada pocos segundos; a INFO taparía el resto.
_QUIET_PATHS = frozenset({"/api/v1/health"})
_SERVER_ERROR = 500
_CLIENT_ERROR = 400

logger = get_logger("resthub.http")


def _incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == _REQUEST_ID_HEADER_KEY:
            candidate = value.decode("latin-1")
            if _VALID_REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return uuid.uuid4().hex


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _log_completed(path: str, status_code: int, duration_ms: float) -> None:
    if status_code >= _SERVER_ERROR:
        log = logger.error
    elif status_code >= _CLIENT_ERROR:
        log = logger.warning
    elif path in _QUIET_PATHS:
        log = logger.debug
    else:
        log = logger.info
    log("request.completed", status=status_code, duration_ms=duration_ms)


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=scope["method"],
            path=scope["path"],
        )
        status_code = _SERVER_ERROR
        start = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((_REQUEST_ID_HEADER_KEY, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception("request.failed", status=_SERVER_ERROR, duration_ms=_elapsed_ms(start))
            raise
        _log_completed(scope["path"], status_code, _elapsed_ms(start))
