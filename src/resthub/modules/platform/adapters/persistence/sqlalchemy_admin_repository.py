from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.timestamps import as_utc
from resthub.modules.platform.adapters.persistence.models import PlatformAdminRow
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.exceptions import EmailAlreadyRegistered


def _to_entity(row: PlatformAdminRow) -> PlatformAdmin:
    return PlatformAdmin(
        id=row.id,
        email=row.email,
        full_name=row.full_name,
        password_hash=row.password_hash,
        is_active=row.is_active,
        created_at=as_utc(row.created_at),
    )


class SqlAlchemyPlatformAdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, admin: PlatformAdmin) -> PlatformAdmin:
        row = PlatformAdminRow(
            email=admin.email,
            full_name=admin.full_name,
            password_hash=admin.password_hash,
            is_active=admin.is_active,
            created_at=admin.created_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # La comprobación del caso de uso es una cortesía; el índice único
            # del correo es el árbitro real.
            await self._session.rollback()
            raise EmailAlreadyRegistered(admin.email) from error
        return _to_entity(row)

    async def get(self, admin_id: int) -> PlatformAdmin | None:
        row = await self._session.get(PlatformAdminRow, admin_id)
        return _to_entity(row) if row else None

    async def get_by_email(self, email: str) -> PlatformAdmin | None:
        row = (
            await self._session.execute(
                select(PlatformAdminRow).where(PlatformAdminRow.email == email)
            )
        ).scalar_one_or_none()
        return _to_entity(row) if row else None
