"""El sumidero de la telemetría: escribe en la base lo que captura el núcleo.

Implementa el puerto del núcleo (`core.telemetry.TelemetrySink`) y lo instala
`main.py` al arrancar. Abre su propia sesión por lote: escribe desde el bucle
de fondo, fuera de toda petición.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.telemetry import EventRecord, RequestRecord
from resthub.modules.platform.adapters.persistence.models import ObsEventRow, ObsRequestRow


class SqlTelemetrySink:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
        async with self._session_factory() as session:
            removed = 0
            for row in (ObsRequestRow, ObsEventRow):
                result = await session.execute(delete(row).where(row.at < before))
                removed += int(getattr(result, "rowcount", 0) or 0)
            await session.commit()
        return removed
