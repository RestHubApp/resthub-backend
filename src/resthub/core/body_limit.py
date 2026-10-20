"""Tamaño máximo del cuerpo de una petición.

Lo más grande que el API recibe de verdad es la foto de un plato (400 KB, en base64 dentro
del JSON: unos 550 KB) y el certificado de SUNAT (64 KB). Sin un tope, cualquiera, aun sin
sesión, podía mandar un cuerpo de cientos de megas y hacer que el servidor lo leyera entero en
memoria antes de que la validación lo rechazara.

Se mira `Content-Length` antes de leer nada y, como un cuerpo por partes (`chunked`) no lo
trae, también se cuentan los bytes a medida que llegan: pasado el tope se corta con un 413 y
no se lee el resto.
"""

from __future__ import annotations

from fastapi import status
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, error_response
from resthub.core.logs import get_logger

BODY_TOO_LARGE_DETAIL = "La petición es demasiado grande."

logger = get_logger("resthub.http")


class RequestBodyTooLargeError(ApiError):
    """El cuerpo pasó el tope mientras se leía.

    Es un `HTTPException`: si la lectura la hizo FastAPI, su manejador de errores ya responde
    el 413 con su `code`; si no, lo atrapa el middleware.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(
            status.HTTP_413_CONTENT_TOO_LARGE, BODY_TOO_LARGE_DETAIL, ErrorCode.REQUEST_TOO_LARGE
        )
        self.limit = limit


def _declared_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length", "")
    return int(value) if value.isdigit() else None


class _Exchange:
    """Cuenta los bytes que entran y anota si ya empezó la respuesta."""

    def __init__(self, receive: Receive, send: Send, max_bytes: int) -> None:
        self._receive = receive
        self._send = send
        self._max_bytes = max_bytes
        self._received = 0
        self.started = False

    async def receive(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            self._received += len(message.get("body", b""))
            if self._received > self._max_bytes:
                raise RequestBodyTooLargeError(self._max_bytes)
        return message

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.started = True
        await self._send(message)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        logger.warning("http.body_too_large", max_bytes=self.max_bytes)
        response = error_response(
            status.HTTP_413_CONTENT_TOO_LARGE, BODY_TOO_LARGE_DETAIL, ErrorCode.REQUEST_TOO_LARGE
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return
        exchange = _Exchange(receive, send, self.max_bytes)
        try:
            await self.app(scope, exchange.receive, exchange.send)
        except RequestBodyTooLargeError:
            # Con la respuesta a medias ya no se puede contestar otra cosa.
            if exchange.started:
                raise
            await self._reject(scope, receive, send)
