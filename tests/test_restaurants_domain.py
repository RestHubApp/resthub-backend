"""Reglas de dominio de restaurantes, sin base ni servidor."""

from __future__ import annotations

import pytest

from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import (
    InvalidRestaurantNameError,
    InvalidSlugError,
    InvalidTimezoneError,
    ReservedSlugError,
    SlugAlreadyTakenError,
)
from resthub.modules.restaurants.use_cases.create_restaurant import (
    CreateRestaurant,
    CreateRestaurantCommand,
)


class InMemoryRestaurants:
    def __init__(self) -> None:
        self.rows: list[Restaurant] = []

    async def add(self, restaurant: Restaurant) -> Restaurant:
        restaurant.id = len(self.rows) + 1
        self.rows.append(restaurant)
        return restaurant

    async def get(self, restaurant_id: int) -> Restaurant | None:
        return next((row for row in self.rows if row.id == restaurant_id), None)

    async def get_by_slug(self, slug: str) -> Restaurant | None:
        return next((row for row in self.rows if row.slug == slug), None)

    async def save(self, restaurant: Restaurant) -> Restaurant:
        return restaurant


def test_by_default_it_is_active_and_in_lima_time() -> None:
    restaurant = Restaurant(name="  Doña   Rosa ", slug="Dona-Rosa")

    assert restaurant.name == "Doña Rosa"
    assert restaurant.slug == "dona-rosa"
    assert restaurant.timezone == "America/Lima"
    assert restaurant.is_active


@pytest.mark.parametrize("slug", ["", "-rosa", "rosa-", "doña-rosa", "dona rosa", "a--b", "x" * 61])
def test_invalid_identifier_is_rejected(slug: str) -> None:
    with pytest.raises(InvalidSlugError):
        Restaurant(name="Doña Rosa", slug=slug)


def test_name_cannot_be_left_empty() -> None:
    with pytest.raises(InvalidRestaurantNameError):
        Restaurant(name="   ", slug="rosa")


def test_time_zone_must_exist() -> None:
    restaurant = Restaurant(name="Doña Rosa", slug="rosa")

    restaurant.move_to_timezone("America/Bogota")
    assert restaurant.timezone == "America/Bogota"
    with pytest.raises(InvalidTimezoneError):
        restaurant.move_to_timezone("Hora de Lima")


async def test_two_restaurants_do_not_share_identifier() -> None:
    create = CreateRestaurant(InMemoryRestaurants())
    await create(CreateRestaurantCommand(name="Doña Rosa", slug="rosa"))

    with pytest.raises(SlugAlreadyTakenError):
        await create(CreateRestaurantCommand(name="Otra Rosa", slug="ROSA"))


@pytest.mark.parametrize("slug", ["muestra", "Muestra", "muestra-archivado-3", "muestrario"])
async def test_real_restaurant_does_not_take_a_sample_restaurant_identifier(
    slug: str,
) -> None:
    with pytest.raises(ReservedSlugError):
        await CreateRestaurant(InMemoryRestaurants())(
            CreateRestaurantCommand(name="Doña Rosa", slug=slug)
        )


async def test_sample_restaurant_does_use_its_prefix() -> None:
    created = await CreateRestaurant(InMemoryRestaurants())(
        CreateRestaurantCommand(name="Muestra", slug="muestra-0a1b2c3d", is_sandbox=True)
    )

    assert created.slug == "muestra-0a1b2c3d"
