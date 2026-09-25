from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import RestaurantNotFound
from resthub.modules.restaurants.ports.restaurant_repository import RestaurantRepository


@dataclass(frozen=True, slots=True)
class UpdateRestaurantCommand:
    restaurant_id: int
    actor_id: int
    # `None` deja el campo como está: la pantalla puede mandar solo lo que cambió.
    name: str | None = None
    timezone: str | None = None


class UpdateRestaurant:
    """El encargado corrige el nombre o la zona horaria de su local.

    El identificador corto no entra: lo fija el alta y puede estar ya en uso
    en enlaces o exportaciones.
    """

    def __init__(self, restaurants: RestaurantRepository, activity: ActivityRecorder) -> None:
        self._restaurants = restaurants
        self._activity = activity

    async def __call__(self, command: UpdateRestaurantCommand) -> Restaurant:
        restaurant = await self._restaurants.get(command.restaurant_id)
        if restaurant is None:
            raise RestaurantNotFound(command.restaurant_id)

        if command.name is not None:
            restaurant.rename(command.name)
        if command.timezone is not None:
            restaurant.move_to_timezone(command.timezone)

        saved = await self._restaurants.save(restaurant)
        await self._activity.record(
            saved.id or command.restaurant_id,
            command.actor_id,
            ActivityKind.RESTAURANT_UPDATED,
            f"{saved.name} ({saved.timezone})",
        )
        return saved
