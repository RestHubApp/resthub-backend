"""Canal de avisos en tiempo real (Server-Sent Events).

El navegador abre una conexión y el servidor le escribe cada vez que cambia algo
que le concierne. Es en un solo sentido a propósito: las acciones siguen yendo
por la API normal, así que no hace falta un WebSocket.

Cada aviso trae solo el tema y el identificador del recurso. Si la cuenta se
desactiva con la conexión abierta, sigue recibiendo esos avisos vacíos hasta
reconectar, pero la API le niega los datos al volver a pedirlos.
"""

from __future__ import annotations

from collections.abc import AsyncIterable

from fastapi import APIRouter
from fastapi.sse import EventSourceResponse, ServerSentEvent

from resthub.core.auth import StreamPrincipalDep
from resthub.core.logs import get_logger
from resthub.core.realtime_broker import BrokerDep

# Cuánto espera el navegador para reconectar si se corta la conexión.
RETRY_MS = 3000

router = APIRouter()
logger = get_logger("resthub.realtime")


@router.get("", response_class=EventSourceResponse, summary="Avisos en tiempo real")
async def stream_events(
    principal: StreamPrincipalDep,
    broker: BrokerDep,
) -> AsyncIterable[ServerSentEvent]:
    async with broker.subscribe() as queue:
        logger.info(
            "realtime.connected",
            user_id=principal.user_id,
            role=principal.role.value,
            restaurant_id=principal.restaurant_id,
        )
        try:
            # El primer mensaje confirma que la suscripción ya está activa: el
            # navegador lo usa para recuperar lo que pudo perderse sin conexión.
            yield ServerSentEvent(event="ready", data={"retry_ms": RETRY_MS}, retry=RETRY_MS)
            while True:
                received = await queue.get()
                if received.is_for(principal):
                    yield ServerSentEvent(event=received.topic, data={"id": received.reference_id})
        finally:
            logger.info("realtime.disconnected", user_id=principal.user_id)
