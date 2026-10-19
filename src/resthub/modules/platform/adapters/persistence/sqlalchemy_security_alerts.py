"""Señales de picos y alertas guardadas (RFS-31).

Los accesos fallidos y frenados salen del historial de accesos del núcleo; los
403, de la telemetría que guarda este módulo (`obs_requests`). Sin telemetría
(`OBSERVABILITY_ENABLED=false`) esa señal queda en cero.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.access_history import AccessHistoryRow, AccessOutcome
from resthub.core.timestamps import as_utc
from resthub.modules.platform.adapters.persistence.models import (
    ObsRequestRow,
    PlatformSecurityAlertRow,
)
from resthub.modules.platform.domain.security_alerts import SecurityAlert, SpikeKind

FORBIDDEN = 403
_OUTCOMES = {
    AccessOutcome.FAILED.value: SpikeKind.FAILED_LOGINS,
    AccessOutcome.THROTTLED.value: SpikeKind.LOCKOUTS,
}


class SqlAlchemySecuritySignals:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_since(self, since: datetime) -> dict[SpikeKind, int]:
        counts = dict.fromkeys(SpikeKind, 0)
        accesses = await self._session.execute(
            select(AccessHistoryRow.outcome, func.count())
            .where(
                AccessHistoryRow.occurred_at >= since,
                AccessHistoryRow.outcome.in_(tuple(_OUTCOMES)),
            )
            .group_by(AccessHistoryRow.outcome)
        )
        for outcome, count in accesses:
            counts[_OUTCOMES[outcome]] = int(count)
        forbidden = await self._session.scalar(
            select(func.count())
            .select_from(ObsRequestRow)
            .where(ObsRequestRow.at >= since, ObsRequestRow.status == FORBIDDEN)
        )
        counts[SpikeKind.FORBIDDEN] = int(forbidden or 0)
        return counts


def _to_entity(row: PlatformSecurityAlertRow) -> SecurityAlert:
    return SecurityAlert(
        id=row.id,
        kind=SpikeKind(row.kind),
        count=row.count,
        threshold=row.threshold,
        window_minutes=row.window_minutes,
        raised_at=as_utc(row.raised_at),
    )


class SqlAlchemySecurityAlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, alert: SecurityAlert) -> SecurityAlert:
        row = PlatformSecurityAlertRow(
            kind=alert.kind.value,
            count=alert.count,
            threshold=alert.threshold,
            window_minutes=alert.window_minutes,
            raised_at=alert.raised_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_entity(row)

    async def last_raised(self, kind: SpikeKind) -> datetime | None:
        last = await self._session.scalar(
            select(func.max(PlatformSecurityAlertRow.raised_at)).where(
                PlatformSecurityAlertRow.kind == kind.value
            )
        )
        return as_utc(last) if last is not None else None

    async def recent(self, limit: int) -> list[SecurityAlert]:
        rows = await self._session.scalars(
            select(PlatformSecurityAlertRow)
            .order_by(PlatformSecurityAlertRow.raised_at.desc(), PlatformSecurityAlertRow.id.desc())
            .limit(limit)
        )
        return [_to_entity(row) for row in rows]
