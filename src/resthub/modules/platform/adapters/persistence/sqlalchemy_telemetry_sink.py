"""El sumidero de la telemetría: escribe en la base lo que captura el núcleo.

Implementa el puerto del núcleo (`core.telemetry.TelemetrySink`) y lo instala
`main.py` al arrancar. Abre su propia sesión por lote: escribe desde el bucle
de fondo, fuera de toda petición.

La purga borra de a `PURGE_BATCH_SIZE` filas por sentencia, cada tanda en su
transacción, para no armar una transacción enorme tras días sin purgar. Con
varios procesos, en PostgreSQL cada tanda toma antes un candado consultivo
(`pg_try_advisory_xact_lock`): si lo tiene otro proceso, este deja la purga
para la próxima vuelta. El candado es de la transacción, así que se suelta solo
al confirmar o si el proceso muere. En SQLite no hace falta.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.telemetry import EventRecord, RequestRecord
from resthub.modules.platform.adapters.persistence.models import ObsEventRow, ObsRequestRow

PURGE_BATCH_SIZE = 10_000
# Identifica el candado de la purga entre todos los consultivos de la base.
PURGE_LOCK_KEY = 0x7265_7374_6875_6201


def purge_guard(dialect_name: str) -> Select[tuple[bool]] | None:
    """La consulta que toma el candado de la purga, o `None` si la base no lo necesita."""
    if dialect_name != "postgresql":
        return None
    return select(func.pg_try_advisory_xact_lock(PURGE_LOCK_KEY))


class SqlTelemetrySink:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        purge_batch_size: int = PURGE_BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._purge_batch_size = purge_batch_size

    async def write(self, requests: Sequence[RequestRecord], events: Sequence[EventRecord]) -> None:
        if not requests and not events:
            return
        async with self._session_factory() as session:
            # Un solo `INSERT` con muchas filas por tabla, no un objeto ORM por fila.
            if requests:
                await session.execute(
                    insert(ObsRequestRow),
                    [
                        {
                            "at": record.at,
                            "method": record.method,
                            "route": record.route,
                            "status": record.status,
                            "duration_ms": record.duration_ms,
                            "db_ms": record.db_ms,
                            "db_queries": record.db_queries,
                            "request_id": record.request_id,
                            "account_kind": record.account_kind,
                            "restaurant_id": record.restaurant_id,
                            "account_id": record.account_id,
                        }
                        for record in requests
                    ],
                )
            if events:
                await session.execute(
                    insert(ObsEventRow),
                    [
                        {
                            "at": record.at,
                            "level": record.level,
                            "logger": record.logger,
                            "event": record.event,
                            "request_id": record.request_id,
                            "restaurant_id": record.restaurant_id,
                            "fields": json.dumps(record.fields, ensure_ascii=False, default=str),
                            "traceback": record.traceback,
                        }
                        for record in events
                    ],
                )
            await session.commit()

    async def purge(self, before: datetime) -> int:
        removed = 0
        for row in (ObsRequestRow, ObsEventRow):
            while True:
                async with self._session_factory() as session:
                    guard = purge_guard(session.get_bind().dialect.name)
                    if guard is not None and not (await session.execute(guard)).scalar():
                        # Otro proceso está purgando.
                        return removed
                    batch = select(row.id).where(row.at < before).limit(self._purge_batch_size)
                    result = await session.execute(
                        delete(row)
                        .where(row.id.in_(batch))
                        .execution_options(synchronize_session=False)
                    )
                    await session.commit()
                deleted = int(getattr(result, "rowcount", 0) or 0)
                removed += deleted
                if deleted < self._purge_batch_size:
                    break
        return removed
