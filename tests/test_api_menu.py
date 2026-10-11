"""El menú por HTTP: permisos, lo que ve cada rol y aislamiento entre locales."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from resthub.core.activity import ActivityKind
from tests.conftest import StaffedRestaurant, authorization_for, url

MENU_URL = url("menu")


async def _category(client: AsyncClient, restaurant: StaffedRestaurant, name: str) -> int:
    response = await client.post(
        f"{MENU_URL}/categories", json={"name": name}, headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _dish(
    client: AsyncClient, restaurant: StaffedRestaurant, category_id: int, name: str, price: str
) -> int:
    response = await client.post(
        f"{MENU_URL}/items",
        json={"category_id": category_id, "name": name, "price": price},
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_menu_is_grouped_by_category_in_order(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    mains = await _category(client, restaurant_a, "Fondos")
    drinks = await _category(client, restaurant_a, "Bebidas")
    await _dish(client, restaurant_a, mains, "Lomo saltado", "28.00")
    await _dish(client, restaurant_a, mains, "Ají de gallina", "22.00")
    await _dish(client, restaurant_a, drinks, "Chicha morada", "5.00")

    response = await client.get(MENU_URL, headers=authorization_for(restaurant_a.waiter))

    assert response.status_code == 200
    categories = response.json()["categories"]
    assert [c["name"] for c in categories] == ["Fondos", "Bebidas"]
    assert [i["name"] for i in categories[0]["items"]] == ["Lomo saltado", "Ají de gallina"]
    assert categories[0]["items"][0]["price"] == "28.00"


async def test_waiter_does_not_see_withdrawn_but_sees_sold_out(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    mains = await _category(client, restaurant_a, "Fondos")
    withdrawn = await _category(client, restaurant_a, "Temporada")
    lomo = await _dish(client, restaurant_a, mains, "Lomo saltado", "28.00")
    stew = await _dish(client, restaurant_a, mains, "Seco de res", "25.00")
    await client.patch(f"{MENU_URL}/items/{stew}", json={"is_active": False}, headers=admin)
    await client.patch(
        f"{MENU_URL}/categories/{withdrawn}", json={"is_active": False}, headers=admin
    )
    sold_out = await client.patch(
        f"{MENU_URL}/items/{lomo}/availability", json={"is_available": False}, headers=admin
    )
    assert sold_out.json()["is_available"] is False

    waiter_response = await client.get(
        MENU_URL, params={"include_inactive": True}, headers=authorization_for(restaurant_a.waiter)
    )
    owner_response = await client.get(MENU_URL, params={"include_inactive": True}, headers=admin)

    assert [c["name"] for c in waiter_response.json()["categories"]] == ["Fondos"]
    dishes = waiter_response.json()["categories"][0]["items"]
    assert [(p["name"], p["is_available"]) for p in dishes] == [("Lomo saltado", False)]
    assert len(owner_response.json()["categories"]) == 2
    assert len(owner_response.json()["categories"][0]["items"]) == 2
    detail_response = await client.get(
        f"{MENU_URL}/items/{stew}", headers=authorization_for(restaurant_a.waiter)
    )
    assert detail_response.status_code == 404


async def test_edit_price_and_move_to_another_category(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    mains = await _category(client, restaurant_a, "Fondos")
    seafood = await _category(client, restaurant_a, "Marinos")
    ceviche = await _dish(client, restaurant_a, mains, "Ceviche", "30.00")

    response = await client.patch(
        f"{MENU_URL}/items/{ceviche}",
        json={"category_id": seafood, "price": "32.50", "description": "Pesca del día"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 200
    assert response.json()["category_id"] == seafood
    assert response.json()["price"] == "32.50"


async def test_duplicate_names_conflict(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    mains = await _category(client, restaurant_a, "Fondos")
    await _dish(client, restaurant_a, mains, "Ceviche", "30.00")

    category = await client.post(f"{MENU_URL}/categories", json={"name": "FONDOS"}, headers=admin)
    dish = await client.post(
        f"{MENU_URL}/items",
        json={"category_id": mains, "name": "ceviche", "price": "30"},
        headers=admin,
    )

    assert category.status_code == 409
    assert dish.status_code == 409


async def test_price_with_three_decimals_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    mains = await _category(client, restaurant_a, "Fondos")

    response = await client.post(
        f"{MENU_URL}/items",
        json={"category_id": mains, "name": "Ceviche", "price": "30.005"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422


async def test_reorder_categories_and_dishes(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    starters = await _category(client, restaurant_a, "Entradas")
    mains = await _category(client, restaurant_a, "Fondos")
    papa = await _dish(client, restaurant_a, starters, "Papa a la huancaína", "12.00")
    causa = await _dish(client, restaurant_a, starters, "Causa limeña", "14.00")

    categories = await client.put(
        f"{MENU_URL}/categories/order", json={"ids": [mains, starters]}, headers=admin
    )
    dishes = await client.put(
        f"{MENU_URL}/categories/{starters}/items/order", json={"ids": [causa, papa]}, headers=admin
    )
    incomplete = await client.put(
        f"{MENU_URL}/categories/order", json={"ids": [mains]}, headers=admin
    )

    assert categories.status_code == 200
    assert dishes.status_code == 200
    assert incomplete.status_code == 422
    menu = (await client.get(MENU_URL, headers=admin)).json()["categories"]
    assert [c["id"] for c in menu] == [mains, starters]
    assert [p["id"] for p in menu[1]["items"]] == [causa, papa]


async def test_only_an_empty_category_can_be_deleted(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    mains = await _category(client, restaurant_a, "Fondos")
    empty = await _category(client, restaurant_a, "Postres")
    await _dish(client, restaurant_a, mains, "Ceviche", "30.00")

    with_dishes = await client.delete(f"{MENU_URL}/categories/{mains}", headers=admin)
    without_dishes = await client.delete(f"{MENU_URL}/categories/{empty}", headers=admin)

    assert with_dishes.status_code == 409
    assert without_dishes.status_code == 204


async def test_changes_are_recorded_in_the_activity_log(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    mains = await _category(client, restaurant_a, "Fondos")
    ceviche = await _dish(client, restaurant_a, mains, "Ceviche", "30.00")
    await client.patch(
        f"{MENU_URL}/items/{ceviche}/availability",
        json={"is_available": False},
        headers=authorization_for(restaurant_a.admin),
    )

    response = await client.get(url("activity"), headers=authorization_for(restaurant_a.admin))

    kinds = [entry["kind"] for entry in response.json()["items"]]
    assert kinds[:3] == [
        ActivityKind.MENU_ITEM_AVAILABILITY_CHANGED.value,
        ActivityKind.MENU_ITEM_CREATED.value,
        ActivityKind.MENU_CATEGORY_CREATED.value,
    ]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/categories", {"name": "Postres"}),
        ("PATCH", "/categories/{category}", {"name": "Otra"}),
        ("DELETE", "/categories/{category}", None),
        ("PUT", "/categories/order", {"ids": []}),
        ("POST", "/items", {"category_id": 1, "name": "X", "price": "1"}),
        ("PATCH", "/items/{item}", {"price": "1.00"}),
        ("PATCH", "/items/{item}/availability", {"is_available": False}),
    ],
)
async def test_waiter_cannot_edit_the_menu(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    category = await _category(client, restaurant_a, "Fondos")
    dish = await _dish(client, restaurant_a, category, "Ceviche", "30.00")
    url = MENU_URL + path.format(category=category, item=dish)

    response = await client.request(
        method, url, json=body, headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/items/{item}", None),
        ("PATCH", "/items/{item}", {"price": "1.00"}),
        ("PATCH", "/items/{item}/availability", {"is_available": False}),
        ("PATCH", "/categories/{category}", {"name": "Intrusa"}),
        ("DELETE", "/categories/{category}", None),
        ("PUT", "/categories/{category}/items/order", {"ids": []}),
    ],
)
async def test_restaurant_cannot_see_or_touch_another_restaurants_menu(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    category = await _category(client, restaurant_b, "Fondos")
    dish = await _dish(client, restaurant_b, category, "Ceviche", "30.00")
    url = MENU_URL + path.format(category=category, item=dish)

    response = await client.request(
        method, url, json=body, headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 404
    menu_a = await client.get(MENU_URL, headers=authorization_for(restaurant_a.admin))
    assert menu_a.json()["categories"] == []
    menu_b = await client.get(MENU_URL, headers=authorization_for(restaurant_b.admin))
    assert menu_b.json()["categories"][0]["items"][0]["price"] == "30.00"


async def test_dish_is_not_created_in_another_restaurants_category(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    foreign = await _category(client, restaurant_b, "Fondos")

    response = await client.post(
        f"{MENU_URL}/items",
        json={"category_id": foreign, "name": "Intruso", "price": "1.00"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 404
