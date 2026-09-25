"""Reparto de avisos en tiempo real.

`LocalBroker` entrega los avisos a las conexiones abiertas en este mismo
proceso. Alcanza con un solo proceso, que es lo que corre en desarrollo y en
las pruebas.

`PostgresBroker` agrega el reparto entre procesos con `LISTEN/NOTIFY`: cada
proceso escucha un canal y publica en él, así un cambio hecho en un proceso
llega a las conexiones abiertas en otro. Evita sumar Redis o RabbitMQ mientras
Postgres ya esté en el proyecto. Si no logra conectarse, avisa en el log y
vuelve al reparto local en vez de dejar la aplicación sin avisos.

Un aviso sale recién cuando la transacción de la petición se confirma. Si
saliera antes, el navegador podría volver a pedir los datos y leer el estado
viejo; si la transacción se deshace, el aviso se descarta.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
from typing import Annotated, Any

import asyncpg
from fastapi import Depends
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from resthub.core.config import get_settings
from resthub.core.database import get_session
from resthub.core.identity import Role
from resthub.core.logs import get_logger
from resthub.core.realtime import RealtimeEvent

CHANNEL = "resthub_events"
_NOTIFY_SQL = "SELECT pg_notify($1, $2)"
# Una conexión que no lee (una pestaña congelada) no puede frenar al resto: sus
# avisos se descartan al llenarse la cola y el navegador recupera al reconectar.
_QUEUE_SIZE = 100
_PENDING_KEY = "resthub.realtime.pending"
_HOOKED_KEY = "resthub.realtime.hooked"

logger = get_logger("resthub.realtime")


def encode_event(event: RealtimeEvent) -> str:
    return json.dumps(
        {
            "restaurant_id": event.restaurant_id,
            "topic": event.topic,
            "user_ids": sorted(event.user_ids),
            "roles": sorted(role.value for role in event.roles),
            "reference_id": event.reference_id,
        }
    )


def decode_event(payload: str) -> RealtimeEvent:
    data: dict[str, Any] = json.loads(payload)
    return RealtimeEvent(
        restaurant_id=int(data["restaurant_id"]),
        topic=str(data["topic"]),
        user_ids=frozenset(int(user_id) for user_id in data["user_ids"]),
        roles=frozenset(Role(role) for role in data["roles"]),
        reference_id=data["reference_id"],
    )


class LocalBroker:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[RealtimeEvent]] = set()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[RealtimeEvent]]:
        queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._queues.add(queue)
        try:
            yield queue
        finally:
            self._queues.discard(queue)

    def deliver(self, event: RealtimeEvent) -> None:
        for queue in tuple(self._queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("realtime.subscriber_lagging", topic=event.topic)

    def publish_soon(self, events: list[RealtimeEvent]) -> None:
        for event in events:
            self.deliver(event)


class PostgresBroker(LocalBroker):
    def __init__(self, dsn: str) -> None:
        super().__init__()
        self._dsn = dsn
        self._connection: asyncpg.Connection | None = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        try:
            connection = await asyncpg.connect(self._dsn)
            await connection.add_listener(CHANNEL, self._on_notification)
            connection.add_termination_listener(self._on_termination)
        except (OSError, asyncpg.PostgresError) as error:
            logger.warning("realtime.listen_failed", error=str(error), fallback="local")
            return
        self._connection = connection
        logger.info("realtime.listening", channel=CHANNEL)

    async def stop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with suppress(OSError, asyncpg.PostgresError):
                await connection.close()

    def publish_soon(self, events: list[RealtimeEvent]) -> None:
        if self._connection is None:
            super().publish_soon(events)
            return
        task = asyncio.get_running_loop().create_task(self._notify(events))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _notify(self, events: list[RealtimeEvent]) -> None:
        connection = self._connection
        if connection is None:
            super().publish_soon(events)
            return
        try:
            # Una conexión de asyncpg no admite dos consultas a la vez.
            async with self._lock:
                for event in events:
                    await connection.execute(_NOTIFY_SQL, CHANNEL, encode_event(event))
        except (OSError, asyncpg.PostgresError) as error:
            logger.warning("realtime.notify_failed", error=str(error), fallback="local")
            super().publish_soon(events)

    def _on_notification(self, _: object, __: int, ___: str, payload: str) -> None:
        self.deliver(decode_event(payload))

    def _on_termination(self, _: object) -> None:
        logger.warning("realtime.listen_lost", fallback="local")
        self._connection = None


def create_broker(database_url: str) -> LocalBroker:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        return LocalBroker()
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    return PostgresBroker(dsn)


@lru_cache
def get_broker() -> LocalBroker:
    return create_broker(get_settings().database_url)


class SessionEventPublisher:
    """Guarda los avisos en la sesión y los suelta al confirmar la transacción."""

    def __init__(self, session: AsyncSession, broker: LocalBroker) -> None:
        self._session = session
        self._broker = broker

    def publish(self, event: RealtimeEvent) -> None:
        sync_session = self._session.sync_session
        # Sin una transacción abierta, un rollback no dispara ningún evento de
        # SQLAlchemy y el aviso quedaría esperando al próximo commit. Abrirla
        # no toca la base: la conexión se pide recién con la primera consulta.
        if not sync_session.in_transaction():
            sync_session.begin()
        info = self._session.info
        info.setdefault(_PENDING_KEY, []).append(event)
        if info.get(_HOOKED_KEY):
            return
        info[_HOOKED_KEY] = True
        broker = self._broker

        def release(session: Session) -> None:
            pending: list[RealtimeEvent] = session.info.pop(_PENDING_KEY, [])
            if pending:
                broker.publish_soon(pending)

        def discard(session: Session, _: object) -> None:
            session.info.pop(_PENDING_KEY, None)

        sqlalchemy_event.listen(sync_session, "after_commit", release)
        # `after_soft_rollback` y no `after_rollback`: el segundo solo se
        # dispara si había una transacción abierta en la base, y un aviso
        # publicado antes de cualquier consulta sobreviviría al rollback.
        sqlalchemy_event.listen(sync_session, "after_soft_rollback", discard)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
BrokerDep = Annotated[LocalBroker, Depends(get_broker)]


def get_event_publisher(session: SessionDep, broker: BrokerDep) -> SessionEventPublisher:
    return SessionEventPublisher(session, broker)


EventPublisherDep = Annotated[SessionEventPublisher, Depends(get_event_publisher)]
