"""Puerto de persistencia de restaurantes."""

from __future__ import annotations

from typing import Protocol

from resthub.modules.restaurants.domain.entities import Restaurant


class RestaurantRepository(Protocol):
    async def add(self, restaurant: Restaurant) -> Restaurant: ...

    async def get(self, restaurant_id: int) -> Restaurant | None: ...

    async def get_by_slug(self, slug: str) -> Restaurant | None: ...

    async def save(self, restaurant: Restaurant) -> Restaurant: ...
