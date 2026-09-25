from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.restaurants.adapters.persistence.mappers import entity_to_row, row_to_entity
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow
from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import RestaurantNotFound, SlugAlreadyTaken


class SqlAlchemyRestaurantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, restaurant: Restaurant) -> Restaurant:
        row = entity_to_row(restaurant)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # La comprobación previa del caso de uso es una cortesía; el índice
            # único del identificador es el único árbitro real.
            await self._session.rollback()
            raise SlugAlreadyTaken(restaurant.slug) from error
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
        row.is_active = restaurant.is_active
        await self._session.flush()
        return row_to_entity(row)
