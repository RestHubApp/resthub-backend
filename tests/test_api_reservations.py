"""Reservas de mesa por HTTP."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.clock import SystemClock
from tests.builders import SeededMenu, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url

RESERVATIONS_URL = url("reservations")
LIMA = "-05:00"


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_a.id)


def _tomorrow(hour: int) -> str:
    """Mañana a esa hora en Lima, como la escribiría el celular."""
    day = (SystemClock().now() - timedelta(hours=5) + timedelta(days=1)).date()
    return f"{day.isoformat()}T{hour:02d}:00:00{LIMA}"


async def _reserve(client: AsyncClient, restaurant: StaffedRestaurant, **data: Any) -> Any:
    body = {"customer_name": "Familia Ríos", "party_size": 6, "phone": "988777666", **data}
    return await client.post(
        RESERVATIONS_URL, json=body, headers=authorization_for(restaurant.waiter)
    )


async def test_same_table_is_not_reserved_twice_at_once(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    first = await _reserve(
        client, restaurant_a, reserved_for=_tomorrow(20), table_id=menu_a.table_1
    )
    overlapping = await _reserve(
        client,
        restaurant_a,
        reserved_for=_tomorrow(21),
        table_id=menu_a.table_1,
        customer_name="Otro",
    )
    later = await _reserve(
        client,
        restaurant_a,
        reserved_for=_tomorrow(22),
        table_id=menu_a.table_1,
        customer_name="Tarde",
    )
    other_table = await _reserve(
        client,
        restaurant_a,
        reserved_for=_tomorrow(21),
        table_id=menu_a.table_2,
        customer_name="Otro",
    )

    assert first.status_code == 201, first.text
    assert overlapping.status_code == 409
    assert "Familia Ríos" in overlapping.json()["detail"]
    # Dos horas por omisión: a las 22 la mesa ya se liberó.
    assert later.status_code == 201
    assert other_table.status_code == 201


async def test_the_days_reservations_in_the_restaurant_zone_and_their_cycle(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    created = await _reserve(client, restaurant_a, reserved_for=_tomorrow(23))
    day = _tomorrow(23)[:10]
    reservation_id = created.json()["id"]

    day_list = await client.get(
        RESERVATIONS_URL, params={"day": day}, headers=authorization_for(restaurant_a.waiter)
    )
    arrived = await client.post(
        f"{RESERVATIONS_URL}/{reservation_id}/status",
        params={"value": "seated"},
        headers=authorization_for(restaurant_a.waiter),
    )
    again = await client.post(
        f"{RESERVATIONS_URL}/{reservation_id}/status",
        params={"value": "no_show"},
        headers=authorization_for(restaurant_a.waiter),
    )

    # 23:00 en Lima son las 04:00 UTC del día siguiente, pero cuenta el día local.
    assert [r["id"] for r in day_list.json()] == [reservation_id]
    assert arrived.json()["status_label"] == "Llegaron"
    assert again.status_code == 422


async def test_cannot_reserve_in_the_past_or_at_a_foreign_table(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    yesterday = (SystemClock().now() - timedelta(days=1)).isoformat()
    foreign_menu = await seed_menu(session, restaurant_b.id)

    past = await _reserve(client, restaurant_a, reserved_for=yesterday)
    no_zone = await _reserve(client, restaurant_a, reserved_for="2030-01-01T20:00:00")
    foreign_table = await _reserve(
        client, restaurant_a, reserved_for=_tomorrow(20), table_id=foreign_menu.table_1
    )

    assert past.status_code == 422
    assert no_zone.status_code == 422
    assert foreign_table.status_code == 404


async def test_cannot_reserve_with_a_customer_of_another_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    foreign_customer = await client.post(
        url("customers"),
        json={"name": "Rosa Díaz", "phone": "977111222", "consent": True},
        headers=authorization_for(restaurant_b.waiter),
    )
    own_customer = await client.post(
        url("customers"),
        json={"name": "Luis Paz", "phone": "977333444", "consent": True},
        headers=authorization_for(restaurant_a.waiter),
    )
    assert foreign_customer.status_code == 201, foreign_customer.text

    with_foreign_customer = await _reserve(
        client, restaurant_a, reserved_for=_tomorrow(20), customer_id=foreign_customer.json()["id"]
    )
    with_own_customer = await _reserve(
        client, restaurant_a, reserved_for=_tomorrow(20), customer_id=own_customer.json()["id"]
    )

    assert with_foreign_customer.status_code == 404
    assert with_own_customer.status_code == 201, with_own_customer.text
    assert with_own_customer.json()["customer_id"] == own_customer.json()["id"]


@pytest.mark.parametrize("phone", ["abc 123 456", "12345", "98877766x", "988-777-666"])
async def test_reservation_phone_follows_the_customer_rule(
    client: AsyncClient, restaurant_a: StaffedRestaurant, phone: str
) -> None:
    rejected = await _reserve(client, restaurant_a, reserved_for=_tomorrow(20), phone=phone)
    empty = await _reserve(client, restaurant_a, reserved_for=_tomorrow(13), phone="")
    spaced = await _reserve(
        client, restaurant_a, reserved_for=_tomorrow(21), phone="+51 988 777 666"
    )

    assert rejected.status_code == 422
    assert empty.status_code == 201, empty.text
    assert spaced.status_code == 201, spaced.text
