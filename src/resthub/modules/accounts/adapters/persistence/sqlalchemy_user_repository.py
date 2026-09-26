from __future__ import annotations

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from resthub.core.pagination import Page
from resthub.modules.accounts.adapters.persistence.mappers import entity_to_row, row_to_entity
from resthub.modules.accounts.adapters.persistence.models import RoleRow, UserRow
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import EmailAlreadyRegistered, UserNotFound
from resthub.modules.accounts.ports.user_repository import UserQuery

_SORTABLE_COLUMNS = {
    "email": UserRow.email,
    "full_name": UserRow.full_name,
    "role": RoleRow.name,
    "created_at": UserRow.created_at,
    "is_active": UserRow.is_active,
}


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user: User) -> User:
        row = entity_to_row(user)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Entre la comprobación del caso de uso y esta inserción cabe otro
            # registro con el mismo correo. La restricción única de la tabla es
            # el único árbitro real, así que su error se traduce al del dominio.
            await self._session.rollback()
            raise EmailAlreadyRegistered(user.email) from error
        await self._session.refresh(row)
        return row_to_entity(row)

    async def get(self, user_id: int) -> User | None:
        row = await self._session.get(UserRow, user_id)
        return row_to_entity(row) if row else None

    async def get_in_restaurant(self, restaurant_id: int, user_id: int) -> User | None:
        result = await self._session.execute(
            select(UserRow).where(UserRow.id == user_id, UserRow.restaurant_id == restaurant_id)
        )
        row = result.scalar_one_or_none()
        return row_to_entity(row) if row else None

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(select(UserRow).where(UserRow.email == email))
        row = result.scalar_one_or_none()
        return row_to_entity(row) if row else None

    async def exists_with_email(self, email: str) -> bool:
        result = await self._session.execute(
            select(func.count()).select_from(UserRow).where(UserRow.email == email)
        )
        return bool(result.scalar_one())

    async def save(self, user: User) -> User:
        row = await self._session.get(UserRow, user.id)
        # El restaurante no se reescribe nunca desde acá: una cuenta no se muda
        # de local, y compararlo evita que un error aguas arriba la mueva.
        if row is None or row.restaurant_id != user.restaurant_id:
            raise UserNotFound(user.id or 0)
        row.email = user.email
        row.full_name = user.full_name
        row.role_id = user.role.id or 0
        row.is_active = user.is_active
        row.password_hash = user.password_hash
        try:
            await self._session.flush()
        except IntegrityError as error:
            await self._session.rollback()
            raise EmailAlreadyRegistered(user.email) from error
        # La relación sigue apuntando al rol anterior hasta releerla.
        await self._session.refresh(row, ["role"])
        return row_to_entity(row)

    async def search(self, query: UserQuery) -> Page[User]:
        # La unión explícita deja ordenar por el nombre del rol; `contains_eager`
        # la reutiliza para cargarlo en vez de sumar otra.
        base = self._apply_filters(
            select(UserRow).join(UserRow.role).options(contains_eager(UserRow.role)), query
        )

        total_result = await self._session.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = int(total_result.scalar_one())

        page = await self._session.execute(
            self._apply_ordering(base, query.ordering).limit(query.limit).offset(query.offset)
        )
        return Page(items=[row_to_entity(row) for row in page.scalars().all()], total=total)

    def _apply_filters(
        self, statement: Select[tuple[UserRow]], query: UserQuery
    ) -> Select[tuple[UserRow]]:
        statement = statement.where(UserRow.restaurant_id == query.restaurant_id)
        if query.ids is not None:
            statement = statement.where(UserRow.id.in_(query.ids))
        if query.role_ids:
            statement = statement.where(UserRow.role_id.in_(query.role_ids))
        if query.search:
            pattern = f"%{query.search}%"
            statement = statement.where(
                or_(UserRow.full_name.ilike(pattern), UserRow.email.ilike(pattern))
            )
        if query.is_active is not None:
            statement = statement.where(UserRow.is_active == query.is_active)
        return statement

    def _apply_ordering(
        self, statement: Select[tuple[UserRow]], ordering: str | None
    ) -> Select[tuple[UserRow]]:
        # El caso de uso ya validó el nombre contra su lista blanca; aquí solo
        # se traduce a columna, y un valor desconocido cae al predeterminado.
        descending = bool(ordering and ordering.startswith("-"))
        name = (ordering or "full_name").removeprefix("-")
        column = _SORTABLE_COLUMNS.get(name, UserRow.full_name)
        return statement.order_by(column.desc() if descending else column.asc(), UserRow.id)
