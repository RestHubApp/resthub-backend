"""Reglas de dominio de restaurantes, sin base ni servidor."""

from __future__ import annotations

import pytest

from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import (
    InvalidRestaurantName,
    InvalidSlug,
    InvalidTimezone,
    ReservedSlug,
    SlugAlreadyTaken,
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


def test_por_omision_esta_activo_y_en_la_hora_de_lima() -> None:
    restaurant = Restaurant(name="  Doña   Rosa ", slug="Dona-Rosa")

    assert restaurant.name == "Doña Rosa"
    assert restaurant.slug == "dona-rosa"
    assert restaurant.timezone == "America/Lima"
    assert restaurant.is_active


@pytest.mark.parametrize("slug", ["", "-rosa", "rosa-", "doña-rosa", "dona rosa", "a--b", "x" * 61])
def test_un_identificador_invalido_se_rechaza(slug: str) -> None:
    with pytest.raises(InvalidSlug):
        Restaurant(name="Doña Rosa", slug=slug)


def test_el_nombre_no_puede_quedar_vacio() -> None:
    with pytest.raises(InvalidRestaurantName):
        Restaurant(name="   ", slug="rosa")


def test_la_zona_horaria_tiene_que_existir() -> None:
    restaurant = Restaurant(name="Doña Rosa", slug="rosa")

    restaurant.move_to_timezone("America/Bogota")
    assert restaurant.timezone == "America/Bogota"
    with pytest.raises(InvalidTimezone):
        restaurant.move_to_timezone("Hora de Lima")


async def test_dos_restaurantes_no_comparten_identificador() -> None:
    alta = CreateRestaurant(InMemoryRestaurants())
    await alta(CreateRestaurantCommand(name="Doña Rosa", slug="rosa"))

    with pytest.raises(SlugAlreadyTaken):
        await alta(CreateRestaurantCommand(name="Otra Rosa", slug="ROSA"))


@pytest.mark.parametrize("slug", ["muestra", "Muestra", "muestra-archivado-3", "muestrario"])
async def test_un_restaurante_real_no_toma_un_identificador_del_local_de_muestra(
    slug: str,
) -> None:
    with pytest.raises(ReservedSlug):
        await CreateRestaurant(InMemoryRestaurants())(
            CreateRestaurantCommand(name="Doña Rosa", slug=slug)
        )


async def test_el_local_de_muestra_si_usa_su_prefijo() -> None:
    creado = await CreateRestaurant(InMemoryRestaurants())(
        CreateRestaurantCommand(name="Muestra", slug="muestra-0a1b2c3d", is_sandbox=True)
    )

    assert creado.slug == "muestra-0a1b2c3d"
