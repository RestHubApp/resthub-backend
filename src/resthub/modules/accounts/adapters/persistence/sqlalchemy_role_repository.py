from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import RoleKind
from resthub.modules.accounts.adapters.persistence.mappers import (
    role_entity_to_row,
    role_row_to_entity,
    sorted_codes,
)
from resthub.modules.accounts.adapters.persistence.models import RoleRow, UserRow
from resthub.modules.accounts.domain.exceptions import RoleInUse, RoleNameTaken, RoleNotFound
from resthub.modules.accounts.domain.roles import Role


class SqlAlchemyRoleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, role: Role) -> Role:
        row = role_entity_to_row(role)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # La restricción única es la que decide entre dos altas simultáneas
            # con el mismo nombre; la comprobación del caso de uso es cortesía.
            await self._session.rollback()
            raise RoleNameTaken(role.name) from error
        return role_row_to_entity(row)

    async def get_in_restaurant(
        self, restaurant_id: int, role_id: int, *, for_update: bool = False
    ) -> Role | None:
        statement = select(RoleRow).where(
            RoleRow.id == role_id, RoleRow.restaurant_id == restaurant_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return role_row_to_entity(row) if row else None

    async def find_by_kind(self, restaurant_id: int, kind: RoleKind) -> Role | None:
        row = (
            await self._session.execute(
                select(RoleRow)
                .where(RoleRow.restaurant_id == restaurant_id, RoleRow.kind == kind.value)
                .order_by(RoleRow.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        return role_row_to_entity(row) if row else None

    async def list_for_restaurant(self, restaurant_id: int) -> list[Role]:
        rows = await self._session.execute(
            select(RoleRow).where(RoleRow.restaurant_id == restaurant_id).order_by(RoleRow.id)
        )
        return [role_row_to_entity(row) for row in rows.scalars().all()]

    async def exists_with_name(
        self, restaurant_id: int, name_key: str, *, excluding_id: int | None = None
    ) -> bool:
        statement = (
            select(func.count())
            .select_from(RoleRow)
            .where(RoleRow.restaurant_id == restaurant_id, RoleRow.name_key == name_key)
        )
        if excluding_id is not None:
            statement = statement.where(RoleRow.id != excluding_id)
        return bool((await self._session.execute(statement)).scalar_one())

    async def member_counts(self, restaurant_id: int) -> dict[int, int]:
        rows = await self._session.execute(
            select(UserRow.role_id, func.count())
            .where(UserRow.restaurant_id == restaurant_id)
            .group_by(UserRow.role_id)
        )
        return {int(role_id): int(count) for role_id, count in rows.all()}

    async def member_ids(self, restaurant_id: int, role_id: int) -> frozenset[int]:
        rows = await self._session.execute(
            select(UserRow.id).where(
                UserRow.restaurant_id == restaurant_id, UserRow.role_id == role_id
            )
        )
        return frozenset(int(user_id) for user_id in rows.scalars().all())

    async def save(self, role: Role) -> Role:
        row = await self._row(role)
        row.name = role.name
        row.name_key = role.name_key
        row.permissions = sorted_codes(role.stored_permissions)
        try:
            await self._session.flush()
        except IntegrityError as error:
            await self._session.rollback()
            raise RoleNameTaken(role.name) from error
        return role_row_to_entity(row)

    async def delete(self, role: Role) -> None:
        row = await self._row(role)
        await self._session.delete(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Alguien le asignó el rol a una cuenta entre la cuenta de miembros
            # y el borrado: la clave foránea lo frena.
            await self._session.rollback()
            raise RoleInUse(role.name) from error

    async def _row(self, role: Role) -> RoleRow:
        row = await self._session.get(RoleRow, role.id)
        # Ni el restaurante ni la clase de rol se reescriben desde acá.
        if row is None or row.restaurant_id != role.restaurant_id:
            raise RoleNotFound(role.id or 0)
        return row
