"""Registro de cada petición HTTP.

Es un middleware ASGI puro y no uno de `BaseHTTPMiddleware`: ese corre la
aplicación en otra tarea, y las variables de contexto que se guardan acá no
llegarían a los logs que escriben los casos de uso.

Cada petición recibe un identificador. Si el frontend manda uno válido en
`X-Request-ID` se respeta, así el error que ve la consola del navegador y el
del servidor se encuentran buscando el mismo valor. Vuelve en la respuesta con
la misma cabecera.

También alimenta la telemetría del panel de observabilidad
(`core/telemetry.py`): al terminar cada respuesta encola método, plantilla de
ruta, estado, duración, tiempo en la base y quién hizo la petición, según lo
que anotaron las dependencias de acceso (`core/request_context.py`). Nunca la
ruta con ids, la query string, las cabeceras ni los cuerpos. Las rutas de
`untracked_paths` no se capturan, ni ellas ni los eventos que dejen. Los 4xx
no dejan su `request.completed` en la telemetría (la fila de la petición ya
tiene el estado) y de las peticiones sin ruta se guarda una muestra
(`core/telemetry.py`).
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Sequence
from contextlib import nullcontext, suppress
from datetime import UTC, datetime

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from resthub.core.db_timing import DbTiming, start_request_timing
from resthub.core.logs import get_logger
from resthub.core.request_context import RequestContext, start_request_context
from resthub.core.telemetry import (
    UNMATCHED_ROUTE,
    RequestRecord,
    get_telemetry,
    normalize_method,
    suppressed_capture,
)

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


def _server_timing(duration_ms: float, db: DbTiming) -> bytes:
    # Cabecera estándar: las herramientas del navegador la muestran en la
    # pestaña de tiempos de cada petición, sin abrir los logs del servidor.
    return (
        f'app;dur={duration_ms}, db;dur={db.milliseconds};desc="{db.queries} consultas"'
    ).encode("latin-1")


def _log_completed(path: str, status_code: int, duration_ms: float, db: DbTiming) -> None:
    if status_code >= _SERVER_ERROR:
        log = logger.error
    elif status_code >= _CLIENT_ERROR:
        log = logger.warning
    elif path in _QUIET_PATHS:
        log = logger.debug
    else:
        log = logger.info
    # Un 4xx es un aviso en la consola, pero no una fila más en `obs_events`:
    # su petición ya se guarda con el estado, y cualquiera puede generarlos sin
    # límite. El 5xx sí queda como evento `error`.
    capture = (
        suppressed_capture() if _CLIENT_ERROR <= status_code < _SERVER_ERROR else nullcontext()
    )
    with capture:
        log(
            "request.completed",
            status=status_code,
            duration_ms=duration_ms,
            db_ms=db.milliseconds,
            db_queries=db.queries,
            db_connects=db.connects,
        )


def route_template(scope: Scope) -> str:
    """La plantilla de la ruta que atendió la petición, con su prefijo.

    FastAPI ya no aplana los routers incluidos: `scope["route"].path` es la
    ruta relativa a su router (`/{order_id}`). La completa está en el contexto
    efectivo que FastAPI deja en el scope; si una versión futura lo quita, se
    cae a la ruta de Starlette, que en una app sin routers anidados ya es la
    completa. `test_observability` fija el resultado esperado.
    """
    effective = scope.get("fastapi", {}).get("effective_route_context")
    path = getattr(effective, "path", None)
    if isinstance(path, str) and path:
        return path
    route_path = getattr(scope.get("route"), "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return UNMATCHED_ROUTE


def _record_request(
    scope: Scope,
    *,
    at: datetime,
    status_code: int,
    duration_ms: float,
    db: DbTiming,
    request_id: str,
    context: RequestContext,
) -> None:
    get_telemetry().record(
        RequestRecord(
            at=at,
            method=normalize_method(scope["method"]),
            route=route_template(scope),
            status=status_code,
            duration_ms=duration_ms,
            db_ms=db.milliseconds,
            db_queries=db.queries,
            request_id=request_id,
            account_kind=context.account_kind,
            restaurant_id=context.restaurant_id,
            account_id=context.account_id,
        )
    )


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp, untracked_paths: Sequence[str] = ()) -> None:
        self.app = app
        self._untracked = tuple(path.rstrip("/") for path in untracked_paths)

    def _tracked(self, path: str) -> bool:
        return not any(
            path == prefix or path.startswith(prefix + "/") for prefix in self._untracked
        )

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
        tracked = self._tracked(scope["path"])
        context = start_request_context(captured=tracked)
        at = datetime.now(UTC)
        start = time.perf_counter()
        db = start_request_timing()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((_REQUEST_ID_HEADER_KEY, request_id.encode("latin-1")))
                headers.append((b"server-timing", _server_timing(_elapsed_ms(start), db)))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # Queda como evento `error` con su traceback y el `request_id` de
            # la petición: el procesador de telemetría lo toma de este log.
            logger.exception("request.failed", status=_SERVER_ERROR, duration_ms=_elapsed_ms(start))
            raise
        else:
            _log_completed(scope["path"], status_code, _elapsed_ms(start), db)
        finally:
            # Anotar la petición nunca puede cambiar su respuesta ni su error.
            if tracked:
                with suppress(Exception):
                    _record_request(
                        scope,
                        at=at,
                        status_code=status_code,
                        duration_ms=_elapsed_ms(start),
                        db=db,
                        request_id=request_id,
                        context=context,
                    )
