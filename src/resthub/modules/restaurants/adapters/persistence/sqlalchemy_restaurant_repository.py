from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.restaurants.adapters.persistence.mappers import entity_to_row, row_to_entity
from resthub.modules.restaurants.adapters.persistence.models import SANDBOX_INDEX, RestaurantRow
from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import (
    RestaurantNotFound,
    RestaurantsError,
    SandboxAlreadyActive,
    SlugAlreadyTaken,
)


def _conflict(error: IntegrityError, restaurant: Restaurant) -> RestaurantsError:
    """Qué índice único chocó, dicho en términos del dominio.

    PostgreSQL nombra el índice (`uq_restaurants_one_active_sandbox`); SQLite,
    la columna (`restaurants.is_sandbox`). Cualquier otro es el del
    identificador corto, el único que queda.
    """
    message = str(error.orig).lower()
    if SANDBOX_INDEX in message or "restaurants.is_sandbox" in message:
        return SandboxAlreadyActive()
    return SlugAlreadyTaken(restaurant.slug)


class SqlAlchemyRestaurantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, restaurant: Restaurant) -> Restaurant:
        row = entity_to_row(restaurant)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # La comprobación previa del caso de uso es una cortesía; los
            # índices únicos (el identificador, un solo local de muestra
            # vigente) son el único árbitro real.
            await self._session.rollback()
            raise _conflict(error, restaurant) from error
        await self._session.refresh(row)
        return row_to_entity(row)

    async def get(self, restaurant_id: int) -> Restaurant | None:
        row = await self._session.get(RestaurantRow, restaurant_id)
        return row_to_entity(row) if row else None

    async def get_by_slug(self, slug: str) -> Restaurant | None:
        result = await self._session.execute(
            select(RestaurantRow).where(RestaurantRow.slug == slug)
        )
        row = result.scalar_one_or_none()
        return row_to_entity(row) if row else None

    async def save(self, restaurant: Restaurant) -> Restaurant:
        row = await self._session.get(RestaurantRow, restaurant.id)
        if row is None:
            raise RestaurantNotFound(restaurant.id or 0)
        row.name = restaurant.name
        row.slug = restaurant.slug
        row.timezone = restaurant.timezone
        row.max_waiter_discount_percent = restaurant.max_waiter_discount_percent
        row.auto_out_of_stock = restaurant.auto_out_of_stock
        row.external_ai_enabled = restaurant.external_ai_enabled
        row.is_active = restaurant.is_active
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Archivar le cambia el identificador: si otro ya lo tiene, es un
            # 409 y no un 500.
            await self._session.rollback()
            raise _conflict(error, restaurant) from error
        return row_to_entity(row)
