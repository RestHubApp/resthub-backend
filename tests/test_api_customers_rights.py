"""Derechos ARCO del cliente por HTTP (Ley N.º 29733): acceso y cancelación."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from tests.builders import SeededMenu, open_cash_session, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url

CUSTOMERS_URL = url("customers")
ORDERS_URL = url("orders")
RESERVATIONS_URL = url("reservations")
ACTIVITY_URL = url("activity")


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    menu = await seed_menu(session, restaurant_a.id)
    await open_cash_session(session, restaurant_a.id, restaurant_a.admin.id or 0)
    return menu


async def _create_customer(client: AsyncClient, restaurant: StaffedRestaurant) -> dict[str, Any]:
    response = await client.post(
        CUSTOMERS_URL,
        json={
            "name": "Ana Torres",
            "phone": "987 654 321",
            "address": "Jr. Pizarro 450",
            "notes": "Alérgica al maní",
            "consent": True,
        },
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _delivery(
    client: AsyncClient,
    restaurant: StaffedRestaurant,
    menu: SeededMenu,
    customer_id: int,
    charge: bool,
) -> dict[str, Any]:
    waiter, owner = authorization_for(restaurant.waiter), authorization_for(restaurant.admin)
    created = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "customer_id": customer_id,
            "notes": "Tocar el timbre dos veces",
            "items": [{"menu_item_id": menu.lomo, "quantity": 1, "notes": "sin maní"}],
        },
        headers=waiter,
    )
    assert created.status_code == 201, created.text
    order = created.json()
    if charge:
        for step, actor in (("send", waiter), ("ready", owner), ("served", waiter)):
            await client.post(f"{ORDERS_URL}/{order['id']}/{step}", headers=actor)
        charge_response = await client.post(
            f"{ORDERS_URL}/{order['id']}/charge", json={"payment_method": "yape"}, headers=waiter
        )
        assert charge_response.status_code == 200, charge_response.text
    return order


async def _create_reservation(
    client: AsyncClient, restaurant: StaffedRestaurant, customer_id: int
) -> None:
    day = (datetime.now(UTC) - timedelta(hours=5) + timedelta(days=1)).date()
    response = await client.post(
        RESERVATIONS_URL,
        json={
            "customer_name": "Ana Torres",
            "phone": "987654321",
            "party_size": 4,
            "reserved_for": f"{day.isoformat()}T20:00:00-05:00",
            "customer_id": customer_id,
            "notes": "Cumpleaños",
        },
        headers=authorization_for(restaurant.waiter),
    )
    assert response.status_code == 201, response.text


async def test_owner_exports_everything_stored_about_the_customer(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    customer = await _create_customer(client, restaurant_a)
    await _delivery(client, restaurant_a, menu_a, customer["id"], charge=True)
    await _create_reservation(client, restaurant_a, customer["id"])
    owner = authorization_for(restaurant_a.admin)

    exported = await client.get(f"{CUSTOMERS_URL}/{customer['id']}/export", headers=owner)
    activity_log = await client.get(ACTIVITY_URL, headers=owner)

    assert exported.status_code == 200, exported.text
    data = exported.json()
    assert (data["name"], data["notes"]) == ("Ana Torres", "Alérgica al maní")
    assert data["consent_at"] is not None
    assert [p["delivery_address"] for p in data["orders"]] == ["Jr. Pizarro 450"]
    assert data["orders"][0]["notes"] == "Tocar el timbre dos veces"
    assert [r["notes"] for r in data["reservations"]] == ["Cumpleaños"]
    # La bitácora anota el pedido sin repetir el nombre.
    assert any(
        e["kind"] == ActivityKind.CUSTOMER_EXPORTED.value
        and e["detail"] == f"Cliente #{customer['id']}"
        for e in activity_log.json()["items"]
    )


async def test_erasing_removes_their_data_from_card_orders_and_reservations(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    customer = await _create_customer(client, restaurant_a)
    order = await _delivery(client, restaurant_a, menu_a, customer["id"], charge=True)
    await _create_reservation(client, restaurant_a, customer["id"])
    owner = authorization_for(restaurant_a.admin)

    erased = await client.post(f"{CUSTOMERS_URL}/{customer['id']}/anonymize", headers=owner)
    card_response = await client.get(f"{CUSTOMERS_URL}/{customer['id']}", headers=owner)
    customers_response = await client.get(CUSTOMERS_URL, headers=owner)
    order_detail = (await client.get(f"{ORDERS_URL}/{order['id']}", headers=owner)).json()
    reservations_response = await client.get(
        RESERVATIONS_URL, headers=owner, params={"day": _tomorrow()}
    )
    # El teléfono queda libre: otra persona puede usarlo.
    other_customer = await client.post(
        CUSTOMERS_URL,
        json={"name": "Otra persona", "phone": "987654321", "consent": True},
        headers=owner,
    )

    assert erased.status_code == 204, erased.text
    assert card_response.status_code == 404
    assert [c["name"] for c in customers_response.json()["items"]] == []
    assert order_detail["customer_name"] == "Cliente eliminado"
    assert (
        order_detail["customer_phone"],
        order_detail["delivery_address"],
        order_detail["notes"],
    ) == (
        "",
        "",
        "",
    )
    assert all(item["notes"] == "" for item in order_detail["items"])
    # Sus ventas siguen contando.
    assert order_detail["status"] == "paid"
    assert [(r["customer_name"], r["phone"], r["notes"]) for r in reservations_response.json()] == [
        ("Cliente eliminado", "", "")
    ]
    assert other_customer.status_code == 201, other_customer.text


def _tomorrow() -> str:
    return (datetime.now(UTC) - timedelta(hours=5) + timedelta(days=1)).date().isoformat()


async def test_with_orders_in_progress_it_is_not_erased(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    # La cocina todavía necesita sus notas (una alergia, por ejemplo).
    customer = await _create_customer(client, restaurant_a)
    await _delivery(client, restaurant_a, menu_a, customer["id"], charge=False)

    erased = await client.post(
        f"{CUSTOMERS_URL}/{customer['id']}/anonymize", headers=authorization_for(restaurant_a.admin)
    )
    card_response = await client.get(
        f"{CUSTOMERS_URL}/{customer['id']}", headers=authorization_for(restaurant_a.admin)
    )

    assert erased.status_code == 409
    assert "pedidos en curso" in erased.json()["detail"]
    assert card_response.json()["name"] == "Ana Torres"


async def test_waiter_cannot_export_or_erase_and_another_restaurant_cannot_see_the_customer(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    customer = await _create_customer(client, restaurant_a)
    waiter = authorization_for(restaurant_a.waiter)
    foreign = authorization_for(restaurant_b.admin)

    responses = [
        await client.get(f"{CUSTOMERS_URL}/{customer['id']}/export", headers=waiter),
        await client.post(f"{CUSTOMERS_URL}/{customer['id']}/anonymize", headers=waiter),
        await client.get(f"{CUSTOMERS_URL}/{customer['id']}/export", headers=foreign),
        await client.post(f"{CUSTOMERS_URL}/{customer['id']}/anonymize", headers=foreign),
    ]

    assert [r.status_code for r in responses] == [403, 403, 404, 404]
