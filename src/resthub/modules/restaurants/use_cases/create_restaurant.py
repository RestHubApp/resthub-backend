from __future__ import annotations

from dataclasses import dataclass

from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import SlugAlreadyTaken
from resthub.modules.restaurants.ports.restaurant_repository import RestaurantRepository


@dataclass(frozen=True, slots=True)
class CreateRestaurantCommand:
    name: str
    slug: str
    timezone: str = DEFAULT_TIMEZONE


class CreateRestaurant:
    """Alta de un restaurante.

    No hay registro público: lo llama `scripts/create_restaurant.py`, que en la
    misma transacción crea también a su primer encargado.
    """

    def __init__(self, restaurants: RestaurantRepository) -> None:
        self._restaurants = restaurants

    async def __call__(self, command: CreateRestaurantCommand) -> Restaurant:
        candidate = Restaurant(name=command.name, slug=command.slug, timezone=command.timezone)
        if await self._restaurants.get_by_slug(candidate.slug) is not None:
            raise SlugAlreadyTaken(candidate.slug)
        return await self._restaurants.add(candidate)
