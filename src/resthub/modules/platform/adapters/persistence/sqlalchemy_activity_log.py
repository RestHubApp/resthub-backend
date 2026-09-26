from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page, PageRequest
from resthub.core.timestamps import as_utc
from resthub.modules.platform.adapters.persistence.models import (
    PlatformActivityRow,
    PlatformAdminRow,
)
from resthub.modules.platform.domain.entities import PlatformActivityKind, PlatformActivityRecord
from resthub.modules.platform.ports.activity_log import PlatformActivityEntry


class SqlAlchemyPlatformActivityLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, admin_id: int, kind: PlatformActivityKind, detail: str = "") -> None:
        entry = PlatformActivityRecord(admin_id=admin_id, kind=kind, detail=detail)
        self._session.add(
            PlatformActivityRow(
                admin_id=entry.admin_id,
                kind=entry.kind.value,
                detail=entry.detail,
                created_at=entry.created_at,
            )
        )
        await self._session.flush()

    async def search(self, page: PageRequest) -> Page[PlatformActivityEntry]:
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(PlatformActivityRow))
            ).scalar_one()
        )
        rows = await self._session.execute(
            select(PlatformActivityRow, PlatformAdminRow.full_name)
            .join(PlatformAdminRow, PlatformAdminRow.id == PlatformActivityRow.admin_id)
            .order_by(PlatformActivityRow.created_at.desc(), PlatformActivityRow.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        return Page(
            items=[
                PlatformActivityEntry(
                    record=PlatformActivityRecord(
                        id=row.id,
                        admin_id=row.admin_id,
                        kind=PlatformActivityKind(row.kind),
                        detail=row.detail,
                        created_at=as_utc(row.created_at),
                    ),
                    admin_name=admin_name,
                )
                for row, admin_name in rows.all()
            ],
            total=total,
        )
