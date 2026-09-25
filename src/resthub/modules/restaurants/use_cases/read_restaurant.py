from __future__ import annotations

from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import RestaurantNotFound
from resthub.modules.restaurants.ports.restaurant_repository import RestaurantRepository


class ReadOwnRestaurant:
    """El restaurante de quien pregunta.

    Recibe el identificador del principal, nunca uno que mande el cliente: no
    hay forma de pedir por esta vía los datos de otro local.
    """

    def __init__(self, restaurants: RestaurantRepository) -> None:
        self._restaurants = restaurants

    async def __call__(self, restaurant_id: int) -> Restaurant:
        restaurant = await self._restaurants.get(restaurant_id)
        if restaurant is None:
            raise RestaurantNotFound(restaurant_id)
        return restaurant
