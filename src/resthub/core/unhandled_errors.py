"""El 500 de un error del programa, con la misma forma y cabeceras que los demás errores.

Sin esto, la excepción que nadie atrapa la convierte en 500 el
`ServerErrorMiddleware` de Starlette, que va siempre por fuera de todos los
middlewares: la respuesta sale en texto plano, sin `{detail, code}`, sin
`X-Request-ID`, sin las cabeceras de seguridad y sin las de CORS. Desde otro
origen el navegador no deja leerla, `fetch` falla como si no hubiera conexión y
el frontend reintenta para siempre un pedido que el servidor rechaza.

Este middleware se registra por dentro de CORS y del registro de peticiones:
atrapa la excepción mientras la respuesta no empezó, la deja en el log como
`request.failed` con su traza (el panel de observabilidad la busca así) y
responde un 500 genérico, sin detalles del error (ASVS V16.5.1). Si la
respuesta ya empezó (un flujo de avisos), no hay cómo cambiarla y la excepción
sigue su camino. Atrapa `Exception` y no `BaseException`: la cancelación del
plazo de la petición no pasa por acá.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from resthub.core.error_codes import ErrorCode
from resthub.core.errors import error_response
from resthub.core.logs import get_logger
from resthub.core.request_context import current_request_context

INTERNAL_ERROR_DETAIL = (
    "Error interno del servidor. Intenta de nuevo; si se repite, avisa con el "
    "identificador de la petición."
)
_SERVER_ERROR = 500

logger = get_logger("resthub.http")


class UnhandledErrorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            if started:
                raise
            logger.exception("request.failed", status=_SERVER_ERROR)
            context = current_request_context()
            if context is not None:
                # La línea de cierre de la petición no se guarda otra vez como evento.
                context.failure_captured = True
            response = error_response(
                _SERVER_ERROR, INTERNAL_ERROR_DETAIL, ErrorCode.INTERNAL_ERROR
            )
            await response(scope, receive, send)
