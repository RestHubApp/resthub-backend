"""El último uso de cada cuenta, leído de las sesiones y del historial de accesos.

Una sesión renovada cuenta como uso aunque nadie vuelva a escribir la
contraseña: el mesero que trabaja todos los días con la misma sesión abierta no
es una cuenta sin uso. El historial cubre los accesos anteriores a que
existieran las sesiones por dispositivo.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.access_history import STAFF_ACCOUNT, AccessHistoryRow, AccessOutcome
from resthub.core.account_sessions import AccountSessionRow
from resthub.core.timestamps import as_utc


class SqlAlchemyAccountActivityReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def last_activity(self, user_ids: Collection[int]) -> dict[int, datetime]:
        if not user_ids:
            return {}
        ids = list(user_ids)
        sessions = select(
            AccountSessionRow.account_id, func.max(AccountSessionRow.last_seen_at)
        ).where(
            AccountSessionRow.account_kind == STAFF_ACCOUNT, AccountSessionRow.account_id.in_(ids)
        )
        accesses = select(
            AccessHistoryRow.account_id, func.max(AccessHistoryRow.occurred_at)
        ).where(
            AccessHistoryRow.account_kind == STAFF_ACCOUNT,
            AccessHistoryRow.account_id.in_(ids),
            AccessHistoryRow.outcome == AccessOutcome.SUCCEEDED.value,
        )
        latest: dict[int, datetime] = {}
        for statement in (
            sessions.group_by(AccountSessionRow.account_id),
            accesses.group_by(AccessHistoryRow.account_id),
        ):
            for account_id, stored in (await self._session.execute(statement)).all():
                if account_id is None or stored is None:
                    continue
                moment = as_utc(stored)
                if account_id not in latest or moment > latest[account_id]:
                    latest[int(account_id)] = moment
        return latest
