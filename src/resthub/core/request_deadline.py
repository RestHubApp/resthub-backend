"""Plazo máximo de una petición.

El frontend da una petición por perdida a los 15 s. Con la base lenta (1 a 3 s
por ida y vuelta en el experimento de caos 02), una lectura de pedidos tardaba
25 s y abrir un pedido 54 s: el cliente ya se había rendido, pero el servidor
seguía trabajando, ocupando conexiones del pool, y a veces terminaba guardando
algo que el mesero no vio confirmado.

Con un plazo propio, menor que el del cliente, el servidor corta primero y
responde un 503 en JSON que la pantalla puede mostrar; la transacción se
deshace. Un pedido que el mesero reintenta lleva el mismo `client_request_id`,
así que, si el corte llegó justo después de confirmar, el reintento devuelve el
mismo pedido en vez de duplicarlo.

Una vez que la petición empezó a confirmar su transacción ya no se corta: se
espera a que termine y el cliente recibe la respuesta real. Cortarla ahí
dejaba un 503 que decía «no terminó la operación» de un cobro o un movimiento
de caja ya guardado, y reintentarlo lo registraba dos veces (solo los pedidos
nuevos llevan `client_request_id`).

Quedan fuera las rutas que esperan a un tercero a propósito (la IA, el
proveedor de comprobantes) y el canal de avisos, que vive abierto.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from contextvars import ContextVar

from sqlalchemy import event
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from resthub.core.logs import get_logger

DEADLINE_DETAIL = (
    "El servidor está tardando más de lo normal y no terminó la operación. "
    "Intenta de nuevo en unos segundos."
)
RETRY_AFTER_SECONDS = 5

logger = get_logger("resthub.http")
# Las peticiones cortadas que todavía limpian: sin una referencia, el recolector
# de basura podría llevarse la tarea a mitad del rollback.
_cleaning: set[asyncio.Task[None]] = set()
# El estado de la petición en curso. La tarea de la petición hereda el contexto
# al crearse, así que el aviso de abajo marca el mismo diccionario que mira el
# middleware. Fuera de una petición (tareas de fondo) queda en `None`.
_request_state: ContextVar[dict[str, bool] | None] = ContextVar(
    "resthub_request_deadline", default=None
)


@event.listens_for(Session, "before_commit")
def _note_commit(_session: Session) -> None:
    state = _request_state.get()
    if state is not None:
        state["committing"] = True


class RequestDeadlineMiddleware:
    def __init__(self, app: ASGIApp, seconds: float, exempt: Sequence[str] = ()) -> None:
        self.app = app
        self.seconds = seconds
        self._exempt = tuple(re.compile(pattern) for pattern in exempt)

    def _applies(self, scope: Scope) -> bool:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            return False
        path: str = scope["path"]
        return not any(pattern.fullmatch(path) for pattern in self._exempt)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        state = {"started": False, "abandoned": False, "committing": False}

        async def guarded_send(message: Message) -> None:
            if state["abandoned"]:
                # Ya se respondió el 503: lo que la petición cortada intente
                # mandar al terminar de limpiar no llega al cliente.
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        # En una tarea aparte para poder responder apenas vence el plazo: la
        # petición cancelada todavía tiene que deshacer su transacción, y con
        # la base lenta eso es otra ida y vuelta que el cliente no tiene por
        # qué esperar.
        token = _request_state.set(state)
        try:
            task = asyncio.ensure_future(self.app(scope, receive, guarded_send))
        finally:
            _request_state.reset(token)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.seconds)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task in done or state["started"]:
            await task
            return
        if state["committing"]:
            # Cancelar a mitad del COMMIT deja sin saber si quedó guardado; se
            # espera y el cliente recibe lo que de verdad pasó.
            logger.warning("request.deadline_waiting_commit", deadline_s=self.seconds)
            await task
            return

        state["abandoned"] = True
        task.cancel()
        _cleaning.add(task)
        task.add_done_callback(_cleaning.discard)
        task.add_done_callback(_log_cleanup_failure)
        logger.error("request.deadline_exceeded", deadline_s=self.seconds)
        response = JSONResponse(
            {"detail": DEADLINE_DETAIL},
            status_code=503,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
        await response(scope, receive, send)


def _log_cleanup_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("request.deadline_cleanup_failed", error=type(error).__name__)
