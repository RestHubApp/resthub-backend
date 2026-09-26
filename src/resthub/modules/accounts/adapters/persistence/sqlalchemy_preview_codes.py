from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.accounts.adapters.persistence.models import PreviewCodeRow
from resthub.modules.accounts.domain.preview import PreviewCode, PreviewGrant


class SqlAlchemyPreviewCodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, code: PreviewCode) -> None:
        self._session.add(
            PreviewCodeRow(
                code_hash=code.code_hash,
                user_id=code.user_id,
                platform_admin_id=code.platform_admin_id,
                expires_at=code.expires_at,
                created_at=code.created_at,
            )
        )
        await self._session.flush()

    async def purge_created_before(self, cutoff: datetime) -> None:
        await self._session.execute(
            delete(PreviewCodeRow)
            .where(PreviewCodeRow.created_at < cutoff)
            .execution_options(synchronize_session=False)
        )

    async def consume(self, code_hash: str, now: datetime) -> PreviewGrant | None:
        # Una sola sentencia decide: marcar usado solo si nadie lo usó y no
        # venció. Con dos canjes a la vez, PostgreSQL hace esperar al segundo
        # hasta que el primero confirme y entonces ya no encuentra la fila
        # libre; SQLite serializa las escrituras. Leer primero y marcar después
        # dejaría una ventana en la que los dos lo ven libre.
        result = await self._session.execute(
            update(PreviewCodeRow)
            .where(
                PreviewCodeRow.code_hash == code_hash,
                PreviewCodeRow.used_at.is_(None),
                PreviewCodeRow.expires_at > now,
            )
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            return None
        row = (
            await self._session.execute(
                select(PreviewCodeRow.user_id, PreviewCodeRow.platform_admin_id).where(
                    PreviewCodeRow.code_hash == code_hash
                )
            )
        ).one()
        return PreviewGrant(user_id=int(row.user_id), platform_admin_id=int(row.platform_admin_id))
