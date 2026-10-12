"""Clientes frecuentes, delivery y pedidos que no se duplican al reintentar."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import SeededMenu, open_cash_session, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url

CUSTOMERS_URL = url("customers")
ORDERS_URL = url("orders")


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    menu = await seed_menu(session, restaurant_a.id)
    await open_cash_session(session, restaurant_a.id, restaurant_a.admin.id or 0)
    return menu


async def _create_customer(
    client: AsyncClient, restaurant: StaffedRestaurant, **fields: Any
) -> dict[str, Any]:
    body = {
        "name": "Ana Torres",
        "phone": "987 654 321",
        "address": "Jr. Pizarro 450",
        "consent": True,
        **fields,
    }
    response = await client.post(
        CUSTOMERS_URL, json=body, headers=authorization_for(restaurant.waiter)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_phone_is_not_repeated_and_is_searched_by_full_number_without_spaces(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    await _create_customer(client, restaurant_a)

    duplicate = await client.post(
        CUSTOMERS_URL,
        json={"name": "Otra", "phone": "987654321", "consent": True},
        headers=authorization_for(restaurant_a.waiter),
    )
    # Otro local puede tener a alguien con el mismo número.
    other_restaurant = await client.post(
        CUSTOMERS_URL,
        json={"name": "Ana", "phone": "987654321", "consent": True},
        headers=authorization_for(restaurant_b.admin),
    )
    search = await client.get(
        CUSTOMERS_URL, params={"q": "987 654 321"}, headers=authorization_for(restaurant_a.waiter)
    )

    assert duplicate.status_code == 409
    assert other_restaurant.status_code == 201
    assert [c["name"] for c in search.json()["items"]] == ["Ana Torres"]


async def test_delivery_requires_phone_and_address(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    without_address = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "customer_name": "Luis",
            "customer_phone": "999111222",
            "items": [{"menu_item_id": menu_a.lomo}],
        },
        headers=authorization_for(restaurant_a.waiter),
    )
    with_table = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "table_id": menu_a.table_1,
            "customer_name": "Luis",
            "customer_phone": "999111222",
            "delivery_address": "Av. España 100",
        },
        headers=authorization_for(restaurant_a.waiter),
    )

    assert without_address.status_code == 422
    assert "dirección" in without_address.json()["detail"]
    assert with_table.status_code == 422


async def test_address_book_customer_completes_the_delivery_and_adds_visits(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    customer = await _create_customer(
        client, restaurant_a, reference="Puerta verde", notes="Alérgica al maní"
    )
    waiter, owner = authorization_for(restaurant_a.waiter), authorization_for(restaurant_a.admin)

    created = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "customer_id": customer["id"],
            "items": [{"menu_item_id": menu_a.lomo, "quantity": 2}],
        },
        headers=waiter,
    )
    order = created.json()
    for step, who in (("send", waiter), ("ready", owner), ("served", waiter)):
        await client.post(f"{ORDERS_URL}/{order['id']}/{step}", headers=who)
    await client.post(
        f"{ORDERS_URL}/{order['id']}/charge", json={"payment_method": "yape"}, headers=waiter
    )
    card = await client.get(f"{CUSTOMERS_URL}/{customer['id']}", headers=waiter)

    assert created.status_code == 201, created.text
    assert order["type_label"] == "Delivery"
    assert (order["customer_name"], order["customer_phone"]) == ("Ana Torres", "987 654 321")
    assert order["delivery_address"] == "Jr. Pizarro 450"
    assert order["delivery_reference"] == "Puerta verde"
    body = card.json()
    assert (body["visits"], body["spent"], body["is_frequent"]) == (1, "56.00", False)
    assert body["recent_orders"][0]["order_id"] == order["id"]


async def test_address_book_phone_identifies_the_customer_without_searching(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    customer = await _create_customer(client, restaurant_a)

    created = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "customer_name": "Ana",
            "customer_phone": "987654321",
            "delivery_address": "Otra dirección 12",
            "items": [{"menu_item_id": menu_a.lomo}],
        },
        headers=authorization_for(restaurant_a.waiter),
    )

    assert created.status_code == 201, created.text
    assert created.json()["customer_id"] == customer["id"]
    # Lo que escribió el mesero vale: hoy lo pidió a otra dirección.
    assert created.json()["delivery_address"] == "Otra dirección 12"


async def test_customer_from_another_restaurant_is_not_usable(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_a: SeededMenu,
) -> None:
    foreign = await client.post(
        CUSTOMERS_URL,
        json={"name": "Ana", "consent": True},
        headers=authorization_for(restaurant_b.admin),
    )

    response = await client.post(
        ORDERS_URL,
        json={"type": "takeaway", "customer_id": foreign.json()["id"]},
        headers=authorization_for(restaurant_a.waiter),
    )
    card = await client.get(
        f"{CUSTOMERS_URL}/{foreign.json()['id']}", headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 404
    assert card.status_code == 404


async def test_mobile_retry_does_not_duplicate_the_order(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    body = {
        "type": "takeaway",
        "client_request_id": "cel-0001-abcdef",
        "items": [{"menu_item_id": menu_a.aji}],
    }

    first = await client.post(ORDERS_URL, json=body, headers=authorization_for(restaurant_a.waiter))
    retry = await client.post(ORDERS_URL, json=body, headers=authorization_for(restaurant_a.waiter))
    active = await client.get(f"{ORDERS_URL}/active", headers=authorization_for(restaurant_a.admin))

    assert first.json()["id"] == retry.json()["id"]
    assert len(active.json()) == 1
