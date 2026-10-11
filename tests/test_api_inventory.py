"""Inventario por HTTP: stock como suma del libro, costos, recetas, permisos y aislamiento."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import SeededMenu, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url

INVENTORY_URL = url("inventory")


async def _ingredient(
    client: AsyncClient,
    restaurant: StaffedRestaurant,
    name: str,
    unit: str = "g",
    min_stock: str = "0",
    unit_cost: str = "0",
) -> dict[str, Any]:
    response = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": name, "unit": unit, "min_stock": min_stock, "unit_cost": unit_cost},
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _post(
    client: AsyncClient, restaurant: StaffedRestaurant, path: str, **body: Any
) -> dict[str, Any]:
    response = await client.post(
        f"{INVENTORY_URL}/{path}", json=body, headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_stock_is_the_sum_of_the_ledger(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    rice = await _ingredient(client, restaurant_a, "Arroz", min_stock="2000")
    assert rice["stock"] == "0.000"

    await _post(
        client,
        restaurant_a,
        "purchases",
        ingredient_id=rice["id"],
        quantity="5000",
        unit_cost="0.004",
    )
    await _post(
        client,
        restaurant_a,
        "waste",
        ingredient_id=rice["id"],
        quantity="250.5",
        reason="Se mojó el saco",
    )
    adjustment = await _post(
        client,
        restaurant_a,
        "adjustments",
        ingredient_id=rice["id"],
        quantity="-49.5",
        reason="Conteo",
    )

    assert adjustment["ingredient"]["stock"] == "4700.000"
    assert adjustment["movement"]["kind"] == "adjustment"
    ledger = await client.get(
        f"{INVENTORY_URL}/movements",
        params={"ingredient_id": rice["id"]},
        headers=authorization_for(restaurant_a.admin),
    )
    quantities = [movement["quantity"] for movement in ledger.json()["items"]]
    assert quantities == ["-49.500", "-250.500", "5000.000"]
    assert ledger.json()["items"][1]["reason"] == "Se mojó el saco"
    assert all(movement["order_number"] is None for movement in ledger.json()["items"])


async def test_count_adjustment_computes_the_difference(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    oil = await _ingredient(client, restaurant_a, "Aceite", unit="ml")
    await _post(
        client,
        restaurant_a,
        "purchases",
        ingredient_id=oil["id"],
        quantity="3000",
        unit_cost="0.009",
    )

    adjustment = await _post(
        client,
        restaurant_a,
        "adjustments",
        ingredient_id=oil["id"],
        counted_stock="2800",
        reason="Conteo del viernes",
    )

    assert adjustment["movement"]["quantity"] == "-200.000"
    assert adjustment["ingredient"]["stock"] == "2800.000"


async def test_purchase_updates_cost_by_weighted_average(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    rice = await _ingredient(client, restaurant_a, "Arroz")

    await _post(
        client,
        restaurant_a,
        "purchases",
        ingredient_id=rice["id"],
        quantity="5000",
        unit_cost="0.004",
    )
    second = await _post(
        client,
        restaurant_a,
        "purchases",
        ingredient_id=rice["id"],
        quantity="20000",
        unit_cost="0.005",
    )

    assert second["ingredient"]["unit_cost"] == "0.004800"
    assert second["movement"]["unit_cost"] == "0.005000"


async def test_waste_requires_a_reason(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    rice = await _ingredient(client, restaurant_a, "Arroz")

    response = await client.post(
        f"{INVENTORY_URL}/waste",
        json={"ingredient_id": rice["id"], "quantity": "10", "reason": ""},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422


async def test_low_stock_alert_with_the_most_urgent_first(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    rice = await _ingredient(client, restaurant_a, "Arroz", min_stock="2000")
    lemon = await _ingredient(client, restaurant_a, "Limón", unit="unit", min_stock="30")
    salt = await _ingredient(client, restaurant_a, "Sal", min_stock="500")
    await _post(
        client,
        restaurant_a,
        "purchases",
        ingredient_id=rice["id"],
        quantity="1500",
        unit_cost="0.004",
    )
    await _post(
        client,
        restaurant_a,
        "purchases",
        ingredient_id=salt["id"],
        quantity="1000",
        unit_cost="0.0015",
    )
    await _post(
        client,
        restaurant_a,
        "adjustments",
        ingredient_id=lemon["id"],
        quantity="-4",
        reason="Faltante",
    )

    response = await client.get(
        f"{INVENTORY_URL}/alerts/low-stock", headers=authorization_for(restaurant_a.admin)
    )

    alerts = response.json()
    assert [(a["name"], a["is_negative"]) for a in alerts] == [
        ("Limón", True),
        ("Arroz", False),
    ]
    assert all(a["is_low"] for a in alerts)


async def test_deactivated_ingredient_neither_alerts_nor_is_purchased(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    rice = await _ingredient(client, restaurant_a, "Arroz", min_stock="2000")
    await client.patch(
        f"{INVENTORY_URL}/ingredients/{rice['id']}", json={"is_active": False}, headers=admin
    )

    alerts = await client.get(f"{INVENTORY_URL}/alerts/low-stock", headers=admin)
    purchase = await client.post(
        f"{INVENTORY_URL}/purchases",
        json={"ingredient_id": rice["id"], "quantity": "10", "unit_cost": "0.004"},
        headers=admin,
    )

    assert alerts.json() == []
    assert purchase.status_code == 409


async def test_recipe_cost_and_margin(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    menu = await seed_menu(session, restaurant_a.id)
    admin = authorization_for(restaurant_a.admin)
    beef = await _ingredient(client, restaurant_a, "Lomo de res", unit_cost="0.045")
    potato = await _ingredient(client, restaurant_a, "Papa amarilla", unit_cost="0.0035")

    saved = await client.put(
        f"{INVENTORY_URL}/recipes/{menu.lomo}",
        json={
            "lines": [
                {"ingredient_id": beef["id"], "quantity": "180"},
                {"ingredient_id": potato["id"], "quantity": "150"},
            ]
        },
        headers=admin,
    )
    read_back = await client.get(f"{INVENTORY_URL}/recipes/{menu.lomo}", headers=admin)
    all_recipes = await client.get(f"{INVENTORY_URL}/recipes", headers=admin)

    assert saved.status_code == 200
    recipe = read_back.json()
    # 180 g x 0.045 + 150 g x 0.0035 = 8.10 + 0.525 = 8.625 → 8.63
    assert recipe["cost"] == "8.63"
    # El margen sale del costo ya redondeado: 28.00 - 8.63, y no 28.00 - 8.625.
    assert recipe["margin"] == "19.37"
    assert recipe["margin_percent"] == "69.2"
    assert [line["cost"] for line in recipe["lines"]] == ["8.1000", "0.5250"]
    by_dish = {row["menu_item_name"]: row for row in all_recipes.json()}
    assert by_dish["Lomo saltado"]["has_recipe"] is True
    assert by_dish["Ají de gallina"]["cost"] is None
    assert by_dish["Ají de gallina"]["margin"] is None


async def test_recipe_is_replaced_whole_and_empty_deletes_it(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    menu = await seed_menu(session, restaurant_a.id)
    admin = authorization_for(restaurant_a.admin)
    beef = await _ingredient(client, restaurant_a, "Lomo de res", unit_cost="0.045")
    url = f"{INVENTORY_URL}/recipes/{menu.lomo}"

    repeated = await client.put(
        url,
        json={
            "lines": [
                {"ingredient_id": beef["id"], "quantity": "100"},
                {"ingredient_id": beef["id"], "quantity": "80"},
            ]
        },
        headers=admin,
    )
    await client.put(
        url, json={"lines": [{"ingredient_id": beef["id"], "quantity": "180"}]}, headers=admin
    )
    empty = await client.put(url, json={"lines": []}, headers=admin)

    assert repeated.status_code == 422
    assert empty.json()["lines"] == []
    assert empty.json()["has_recipe"] is False


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/ingredients", None),
        ("GET", "/alerts/low-stock", None),
        ("GET", "/movements", None),
        ("GET", "/recipes", None),
        ("POST", "/ingredients", {"name": "Sal", "unit": "g"}),
        ("POST", "/purchases", {"ingredient_id": 1, "quantity": "1", "unit_cost": "1"}),
        ("POST", "/waste", {"ingredient_id": 1, "quantity": "1", "reason": "x"}),
        ("POST", "/adjustments", {"ingredient_id": 1, "quantity": "1", "reason": "x"}),
        ("PUT", "/recipes/1", {"lines": []}),
    ],
)
async def test_waiter_cannot_enter_inventory(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    response = await client.request(
        method, INVENTORY_URL + path, json=body, headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/ingredients/{id}", None),
        ("PATCH", "/ingredients/{id}", {"name": "Intruso"}),
        ("POST", "/purchases", {"ingredient_id": "{id}", "quantity": "1", "unit_cost": "1"}),
        ("POST", "/waste", {"ingredient_id": "{id}", "quantity": "1", "reason": "x"}),
        ("POST", "/adjustments", {"ingredient_id": "{id}", "quantity": "1", "reason": "x"}),
    ],
)
async def test_restaurant_neither_sees_nor_touches_another_ones_ingredients(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    foreign = await _ingredient(client, restaurant_b, "Arroz")
    if body is not None:
        body = {key: foreign["id"] if value == "{id}" else value for key, value in body.items()}

    response = await client.request(
        method,
        INVENTORY_URL + path.format(id=foreign["id"]),
        json=body,
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 404
    own = await client.get(
        f"{INVENTORY_URL}/ingredients/{foreign['id']}",
        headers=authorization_for(restaurant_b.admin),
    )
    assert own.json()["name"] == "Arroz"
    assert own.json()["stock"] == "0.000"
    listing_a = await client.get(
        f"{INVENTORY_URL}/ingredients", headers=authorization_for(restaurant_a.admin)
    )
    assert listing_a.json() == []


async def test_recipes_do_not_cross_restaurants(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    menu_a: SeededMenu = await seed_menu(session, restaurant_a.id)
    menu_b: SeededMenu = await seed_menu(session, restaurant_b.id)
    admin_a = authorization_for(restaurant_a.admin)
    own = await _ingredient(client, restaurant_a, "Carne")
    foreign = await _ingredient(client, restaurant_b, "Carne")

    foreign_dish = await client.put(
        f"{INVENTORY_URL}/recipes/{menu_b.lomo}",
        json={"lines": [{"ingredient_id": own["id"], "quantity": "1"}]},
        headers=admin_a,
    )
    foreign_ingredient = await client.put(
        f"{INVENTORY_URL}/recipes/{menu_a.lomo}",
        json={"lines": [{"ingredient_id": foreign["id"], "quantity": "1"}]},
        headers=admin_a,
    )
    read_foreign = await client.get(f"{INVENTORY_URL}/recipes/{menu_b.lomo}", headers=admin_a)

    assert foreign_dish.status_code == 404
    assert foreign_ingredient.status_code == 404
    assert read_foreign.status_code == 404
