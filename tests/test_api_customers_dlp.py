"""Prevención de fugas internas: acceso a fichas, enmascarado y alerta."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.config import get_settings
from resthub.core.permissions import Permission
from tests.conftest import StaffedRestaurant, authorization_for, staff_with_role, url

CUSTOMERS_URL = url("customers")
ACTIVITY_URL = url("activity")


async def _create_customer(
    client: AsyncClient, restaurant: StaffedRestaurant, phone: str = "987654222"
) -> int:
    response = await client.post(
        CUSTOMERS_URL,
        json={
            "name": "Ana Torres",
            "phone": phone,
            "email": "ana@correo.pe",
            "address": "Jr. Pizarro 450",
            "consent": True,
        },
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _entries(
    client: AsyncClient, restaurant: StaffedRestaurant, kind: ActivityKind
) -> list[dict[str, Any]]:
    response = await client.get(
        ACTIVITY_URL, params={"kind": kind.value}, headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


@pytest.fixture
async def reader_only(session: AsyncSession, restaurant_a: StaffedRestaurant) -> dict[str, str]:
    """Una cuenta que busca y ve clientes pero no los gestiona."""
    user = await staff_with_role(
        session, restaurant_a, "Solo lectura", {Permission.CUSTOMERS_READ.value}
    )
    return authorization_for(user)


async def test_opening_a_card_is_logged_with_who_and_which_customer(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    customer_id = await _create_customer(client, restaurant_a)

    response = await client.get(
        f"{CUSTOMERS_URL}/{customer_id}", headers=authorization_for(restaurant_a.waiter)
    )

    entries = await _entries(client, restaurant_a, ActivityKind.CUSTOMER_VIEWED)
    assert response.status_code == 200
    assert len(entries) == 1
    assert entries[0]["user_id"] == restaurant_a.waiter.id
    assert entries[0]["detail"] == f"Cliente #{customer_id}"


async def test_create_and_update_do_not_count_as_opening_a_card(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    customer_id = await _create_customer(client, restaurant_a)
    await client.put(
        f"{CUSTOMERS_URL}/{customer_id}",
        json={"name": "Ana Torres", "phone": "987654222"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert await _entries(client, restaurant_a, ActivityKind.CUSTOMER_VIEWED) == []


async def test_manager_sees_contact_data_in_full(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    customer_id = await _create_customer(client, restaurant_a)

    card = (
        await client.get(
            f"{CUSTOMERS_URL}/{customer_id}", headers=authorization_for(restaurant_a.waiter)
        )
    ).json()

    assert card["masked"] is False
    assert card["phone"] == "987654222"
    assert card["email"] == "ana@correo.pe"


async def test_account_without_manage_gets_masked_data_in_list_and_card(
    client: AsyncClient, restaurant_a: StaffedRestaurant, reader_only: dict[str, str]
) -> None:
    customer_id = await _create_customer(client, restaurant_a)

    listed = (await client.get(CUSTOMERS_URL, headers=reader_only)).json()["items"][0]
    card = (await client.get(f"{CUSTOMERS_URL}/{customer_id}", headers=reader_only)).json()

    for shown in (listed, card):
        assert shown["masked"] is True
        assert shown["phone"] == "987•••222"
        assert shown["email"] == "a•••@correo.pe"


async def test_reveal_returns_full_data_and_is_logged(
    client: AsyncClient, restaurant_a: StaffedRestaurant, reader_only: dict[str, str]
) -> None:
    customer_id = await _create_customer(client, restaurant_a)

    response = await client.post(f"{CUSTOMERS_URL}/{customer_id}/reveal", headers=reader_only)

    assert response.status_code == 200
    assert response.json() == {"phone": "987654222", "email": "ana@correo.pe"}
    assert len(await _entries(client, restaurant_a, ActivityKind.CUSTOMER_REVEALED)) == 1


async def test_reveal_needs_read_permission_and_hides_other_restaurants(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    customer_id = await _create_customer(client, restaurant_a)
    nobody = authorization_for(await staff_with_role(session, restaurant_a, "Sin clientes", set()))

    forbidden = await client.post(f"{CUSTOMERS_URL}/{customer_id}/reveal", headers=nobody)
    other_local = await client.post(
        f"{CUSTOMERS_URL}/{customer_id}/reveal", headers=authorization_for(restaurant_b.admin)
    )

    assert forbidden.status_code == 403
    assert other_local.status_code == 404
    assert await _entries(client, restaurant_a, ActivityKind.CUSTOMER_REVEALED) == []


@pytest.fixture
def low_threshold(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DLP_ALERT_MAX_CARDS", "2")
    monkeypatch.setenv("DLP_ALERT_WINDOW_MINUTES", "10")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("low_threshold")
async def test_opening_too_many_cards_raises_one_alert_for_that_account(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    ids = [
        await _create_customer(client, restaurant_a, phone=f"98765422{digit}") for digit in range(4)
    ]
    waiter = authorization_for(restaurant_a.waiter)

    for customer_id in ids[:2]:
        await client.get(f"{CUSTOMERS_URL}/{customer_id}", headers=waiter)
    assert await _entries(client, restaurant_a, ActivityKind.DATA_ACCESS_ALERT) == []

    for customer_id in ids[2:]:
        await client.get(f"{CUSTOMERS_URL}/{customer_id}", headers=waiter)

    alerts = await _entries(client, restaurant_a, ActivityKind.DATA_ACCESS_ALERT)
    assert len(alerts) == 1
    assert alerts[0]["user_id"] == restaurant_a.waiter.id
    assert alerts[0]["detail"] == "3 fichas en 10 min"


@pytest.mark.usefixtures("low_threshold")
async def test_exports_count_toward_the_alert_and_other_accounts_do_not(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    ids = [
        await _create_customer(client, restaurant_a, phone=f"98765422{digit}") for digit in range(3)
    ]
    owner = authorization_for(restaurant_a.admin)

    for customer_id in ids:
        exported = await client.get(f"{CUSTOMERS_URL}/{customer_id}/export", headers=owner)
        assert exported.status_code == 200, exported.text
    await client.get(f"{CUSTOMERS_URL}/{ids[0]}", headers=authorization_for(restaurant_a.waiter))

    alerts = await _entries(client, restaurant_a, ActivityKind.DATA_ACCESS_ALERT)
    assert [alert["user_id"] for alert in alerts] == [restaurant_a.admin.id]
