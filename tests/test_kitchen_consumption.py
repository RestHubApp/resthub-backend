"""Consumo automático de insumos al servir: la conexión entre pedidos e inventario."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.inventory.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyIngredientRepository,
    SqlAlchemyRecipeRepository,
    SqlAlchemyStockLedgerRepository,
)
from resthub.modules.inventory.use_cases.consume_served_order import (
    ConsumeServedOrder,
    ConsumeServedOrderCommand,
    ServedDish,
)
from tests.builders import SeededMenu, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url

ORDERS_URL = url("orders")
INVENTORY_URL = url("inventory")


@dataclass(frozen=True, slots=True)
class Kitchen:
    menu: SeededMenu
    meat: int
    papa: int
    chicha: int


@pytest.fixture
async def kitchen(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> Kitchen:
    """Lomo con receta de carne y papa, chicha con receta y sin stock, ají sin receta."""
    menu = await seed_menu(session, restaurant_a.id)
    admin = authorization_for(restaurant_a.admin)

    async def create_ingredient(name: str, unit: str, purchase: str | None) -> int:
        created = await client.post(
            f"{INVENTORY_URL}/ingredients",
            json={"name": name, "unit": unit, "min_stock": "100"},
            headers=admin,
        )
        ingredient_id = int(created.json()["id"])
        if purchase is not None:
            await client.post(
                f"{INVENTORY_URL}/purchases",
                json={"ingredient_id": ingredient_id, "quantity": purchase, "unit_cost": "0.01"},
                headers=admin,
            )
        return ingredient_id

    meat = await create_ingredient("Lomo de res", "g", "1000")
    papa = await create_ingredient("Papa amarilla", "g", "1000")
    chicha = await create_ingredient("Chicha morada preparada", "ml", None)

    async def set_recipe(dish: int, *lines: tuple[int, str]) -> None:
        response = await client.put(
            f"{INVENTORY_URL}/recipes/{dish}",
            json={"lines": [{"ingredient_id": i, "quantity": q} for i, q in lines]},
            headers=admin,
        )
        assert response.status_code == 200, response.text

    await set_recipe(menu.lomo, (meat, "180"), (papa, "150"))
    await set_recipe(menu.chicha, (chicha, "400"))
    return Kitchen(menu=menu, meat=meat, papa=papa, chicha=chicha)


async def _stock(client: AsyncClient, local: StaffedRestaurant, ingredient_id: int) -> str:
    response = await client.get(
        f"{INVENTORY_URL}/ingredients/{ingredient_id}", headers=authorization_for(local.admin)
    )
    return str(response.json()["stock"])


async def _consumptions(client: AsyncClient, local: StaffedRestaurant, order_id: int) -> list[Any]:
    response = await client.get(
        f"{INVENTORY_URL}/movements",
        params={"order_id": order_id, "kind": "consumption"},
        headers=authorization_for(local.admin),
    )
    return list(response.json()["items"])


async def _until_served(client: AsyncClient, local: StaffedRestaurant, order_id: int) -> None:
    waiter, owner = authorization_for(local.waiter), authorization_for(local.admin)
    for action, headers in (("ready", owner), ("served", waiter)):
        response = await client.post(f"{ORDERS_URL}/{order_id}/{action}", headers=headers)
        assert response.status_code == 200, response.text


async def _place_order(
    client: AsyncClient, local: StaffedRestaurant, kitchen: Kitchen, *items: tuple[int, int]
) -> int:
    waiter = authorization_for(local.waiter)
    created = await client.post(
        ORDERS_URL,
        json={
            "type": "dine_in",
            "table_id": kitchen.menu.table_1,
            "items": [{"menu_item_id": d, "quantity": q} for d, q in items],
        },
        headers=waiter,
    )
    order_id = int(created.json()["id"])
    await client.post(f"{ORDERS_URL}/{order_id}/send", headers=waiter)
    return order_id


async def test_serving_deducts_according_to_recipes(
    client: AsyncClient, restaurant_a: StaffedRestaurant, kitchen: Kitchen
) -> None:
    order_id = await _place_order(
        client, restaurant_a, kitchen, (kitchen.menu.lomo, 2), (kitchen.menu.aji, 1)
    )
    assert await _stock(client, restaurant_a, kitchen.meat) == "1000.000"

    await _until_served(client, restaurant_a, order_id)

    assert await _stock(client, restaurant_a, kitchen.meat) == "640.000"
    assert await _stock(client, restaurant_a, kitchen.papa) == "700.000"
    consumptions = await _consumptions(client, restaurant_a, order_id)
    assert sorted(c["quantity"] for c in consumptions) == ["-300.000", "-360.000"]
    assert {c["created_by"] for c in consumptions} == {restaurant_a.waiter.id}
    assert all(c["unit_cost"] == "0.010000" for c in consumptions)
    order_response = await client.get(
        f"{ORDERS_URL}/{order_id}", headers=authorization_for(restaurant_a.admin)
    )
    assert {c["order_number"] for c in consumptions} == {order_response.json()["number"]}


async def test_dish_without_recipe_deducts_nothing(
    client: AsyncClient, restaurant_a: StaffedRestaurant, kitchen: Kitchen
) -> None:
    order_id = await _place_order(client, restaurant_a, kitchen, (kitchen.menu.aji, 3))

    await _until_served(client, restaurant_a, order_id)

    assert await _consumptions(client, restaurant_a, order_id) == []


async def test_serving_again_deducts_only_the_new(
    client: AsyncClient, restaurant_a: StaffedRestaurant, kitchen: Kitchen
) -> None:
    order_id = await _place_order(client, restaurant_a, kitchen, (kitchen.menu.lomo, 1))
    await _until_served(client, restaurant_a, order_id)

    second_round = await client.post(
        f"{ORDERS_URL}/{order_id}/items",
        json={"items": [{"menu_item_id": kitchen.menu.lomo, "quantity": 2}]},
        headers=authorization_for(restaurant_a.waiter),
    )
    assert second_round.json()["status"] == "in_kitchen"
    await _until_served(client, restaurant_a, order_id)

    # 180 por la primera porción y 360 por las dos nuevas; nada dos veces.
    assert await _stock(client, restaurant_a, kitchen.meat) == "460.000"
    assert len(await _consumptions(client, restaurant_a, order_id)) == 4


async def test_consumption_is_idempotent_even_if_it_arrives_twice(
    session: AsyncSession, client: AsyncClient, restaurant_a: StaffedRestaurant, kitchen: Kitchen
) -> None:
    order_id = await _place_order(client, restaurant_a, kitchen, (kitchen.menu.lomo, 1))
    await _until_served(client, restaurant_a, order_id)
    detail = await client.get(
        f"{ORDERS_URL}/{order_id}", headers=authorization_for(restaurant_a.admin)
    )
    item_id = detail.json()["items"][0]["id"]
    consume = ConsumeServedOrder(
        SqlAlchemyIngredientRepository(session),
        SqlAlchemyStockLedgerRepository(session),
        SqlAlchemyRecipeRepository(session),
    )

    repeated = await consume(
        ConsumeServedOrderCommand(
            restaurant_id=restaurant_a.id,
            order_id=order_id,
            actor_id=restaurant_a.admin.id or 0,
            dishes=(ServedDish(order_item_id=item_id, menu_item_id=kitchen.menu.lomo, portions=1),),
        )
    )

    assert repeated.movements == []
    stock = await SqlAlchemyStockLedgerRepository(session).stock_of(restaurant_a.id, [kitchen.meat])
    assert stock[kitchen.meat] == Decimal("820.000")


async def test_negative_stock_is_allowed_and_reported(
    client: AsyncClient, restaurant_a: StaffedRestaurant, kitchen: Kitchen
) -> None:
    # Con los platos sin insumos agotándose solos no se podría pedir; un local
    # que todavía no lleva el stock al día lo apaga y el plato sale igual.
    await client.patch(
        url("restaurant"),
        json={"auto_out_of_stock": False},
        headers=authorization_for(restaurant_a.admin),
    )
    order_id = await _place_order(client, restaurant_a, kitchen, (kitchen.menu.chicha, 2))

    await _until_served(client, restaurant_a, order_id)

    assert await _stock(client, restaurant_a, kitchen.chicha) == "-800.000"
    alerts = await client.get(
        f"{INVENTORY_URL}/alerts/low-stock", headers=authorization_for(restaurant_a.admin)
    )
    first = alerts.json()[0]
    assert (first["id"], first["is_negative"]) == (kitchen.chicha, True)


async def test_cancelling_served_does_not_return_ingredients(
    client: AsyncClient, restaurant_a: StaffedRestaurant, kitchen: Kitchen
) -> None:
    order_id = await _place_order(client, restaurant_a, kitchen, (kitchen.menu.lomo, 1))
    await _until_served(client, restaurant_a, order_id)

    await client.post(
        f"{ORDERS_URL}/{order_id}/cancel",
        json={"reason": "El cliente se fue sin pagar"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert await _stock(client, restaurant_a, kitchen.meat) == "820.000"
