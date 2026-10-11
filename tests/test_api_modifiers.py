"""Opciones de los platos y platos agotados por falta de insumos, por HTTP."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import SeededMenu, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url

MENU_URL = url("menu")
ORDERS_URL = url("orders")

SIZE = {
    "name": "Tamaño",
    "min_choices": 1,
    "max_choices": 1,
    "options": [{"name": "Personal", "price": "0"}, {"name": "Familiar", "price": "10.50"}],
}
EXTRAS = {
    "name": "Extras",
    "min_choices": 0,
    "max_choices": 2,
    "options": [
        {"name": "Huevo frito", "price": "2"},
        {"name": "Plátano", "price": "2.50"},
        {"name": "Sin cebolla", "price": "0"},
    ],
}


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_a.id)


async def _with_options(
    client: AsyncClient, restaurant: StaffedRestaurant, dish: int
) -> dict[str, Any]:
    response = await client.patch(
        f"{MENU_URL}/items/{dish}",
        json={"modifier_groups": [SIZE, EXTRAS]},
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _place_order(
    client: AsyncClient, restaurant: StaffedRestaurant, dish: int, modifiers: list[dict[str, str]]
) -> Any:
    return await client.post(
        ORDERS_URL,
        json={
            "type": "takeaway",
            "items": [{"menu_item_id": dish, "quantity": 2, "modifiers": modifiers}],
        },
        headers=authorization_for(restaurant.waiter),
    )


async def test_dish_stores_its_option_groups(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    dish_data = await _with_options(client, restaurant_a, menu_a.lomo)
    waiter_menu = await client.get(MENU_URL, headers=authorization_for(restaurant_a.waiter))

    assert [g["name"] for g in dish_data["modifier_groups"]] == ["Tamaño", "Extras"]
    assert dish_data["modifier_groups"][0]["options"][1] == {"name": "Familiar", "price": "10.50"}
    lomo = next(
        item
        for section in waiter_menu.json()["categories"]
        for item in section["items"]
        if item["id"] == menu_a.lomo
    )
    assert lomo["modifier_groups"][1]["max_choices"] == 2


async def test_malformed_groups_are_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    bad_group = {**EXTRAS, "min_choices": 3, "max_choices": 2}

    response = await client.patch(
        f"{MENU_URL}/items/{menu_a.lomo}",
        json={"modifier_groups": [bad_group]},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422


async def test_chosen_options_add_to_price_and_are_frozen(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _with_options(client, restaurant_a, menu_a.lomo)

    response = await _place_order(
        client,
        restaurant_a,
        menu_a.lomo,
        [
            {"group": "Tamaño", "option": "Familiar"},
            {"group": "Extras", "option": "Plátano"},
            {"group": "extras", "option": "sin cebolla"},
        ],
    )

    item = response.json()["items"][0]
    # 28.00 + 10.50 + 2.50 = 41.00 por plato; dos platos.
    assert item["unit_price"] == "41.00"
    assert response.json()["total"] == "82.00"
    assert [(m["option"], m["price"]) for m in item["modifiers"]] == [
        ("Familiar", "10.50"),
        ("Plátano", "2.50"),
        ("Sin cebolla", "0.00"),
    ]


@pytest.mark.parametrize(
    "modifiers",
    [
        [],
        [{"group": "Tamaño", "option": "Mediano"}],
        [{"group": "Término", "option": "Tres cuartos"}],
        [
            {"group": "Tamaño", "option": "Personal"},
            {"group": "Extras", "option": "Huevo frito"},
            {"group": "Extras", "option": "Plátano"},
            {"group": "Extras", "option": "Sin cebolla"},
        ],
    ],
    ids=["falta-el-obligatorio", "opcion-inexistente", "grupo-inexistente", "pasa-el-maximo"],
)
async def test_invalid_choice_cannot_be_ordered(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    modifiers: list[dict[str, str]],
) -> None:
    await _with_options(client, restaurant_a, menu_a.lomo)

    response = await _place_order(client, restaurant_a, menu_a.lomo, modifiers)

    assert response.status_code == 422


async def test_dish_without_ingredients_sells_out_by_itself(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    owner = authorization_for(restaurant_a.admin)
    ingredient = await client.post(
        url("inventory/ingredients"),
        json={"name": "Carne de res", "unit": "g", "min_stock": "0", "unit_cost": "0.04"},
        headers=owner,
    )
    ingredient_id = ingredient.json()["id"]
    await client.put(
        url(f"inventory/recipes/{menu_a.lomo}"),
        json={"lines": [{"ingredient_id": ingredient_id, "quantity": "180"}]},
        headers=owner,
    )

    out_of_stock = await _place_order(client, restaurant_a, menu_a.lomo, [])
    empty_menu = await client.get(MENU_URL, headers=authorization_for(restaurant_a.waiter))
    await client.post(
        url("inventory/purchases"),
        json={"ingredient_id": ingredient_id, "quantity": "500", "unit_cost": "0.04"},
        headers=owner,
    )
    in_stock = await _place_order(client, restaurant_a, menu_a.lomo, [])

    def sold_out(menu: Any) -> bool:
        return next(
            item["out_of_stock"]
            for section in menu["categories"]
            for item in section["items"]
            if item["id"] == menu_a.lomo
        )

    assert out_of_stock.status_code == 409
    assert sold_out(empty_menu.json()) is True
    assert in_stock.status_code == 201
    # Con la compra, el plato vuelve a la carta sin que nadie lo toque.
    assert sold_out((await client.get(MENU_URL, headers=owner)).json()) is False
