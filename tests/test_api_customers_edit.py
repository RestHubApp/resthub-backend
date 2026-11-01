"""Edición y búsqueda de la libreta de clientes, por HTTP y en el repositorio."""

from __future__ import annotations

from dataclasses import replace

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.crypto import get_cipher
from resthub.modules.customers.adapters.persistence.models import CustomerRow
from resthub.modules.customers.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyCustomerRepository,
)
from resthub.modules.customers.domain.customers import Customer
from resthub.modules.customers.domain.exceptions import PhoneTakenError
from tests.conftest import StaffedRestaurant, authorization_for, url

CUSTOMERS_URL = url("customers")


async def _create(client: AsyncClient, restaurant: StaffedRestaurant, **fields: str) -> int:
    response = await client.post(
        CUSTOMERS_URL,
        json={"name": "Ana Torres", "consent": True, **fields},
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_owner_edits_card_and_search_reflects_it(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    ana = await _create(client, restaurant_a, phone="987 654 321")
    await _create(client, restaurant_a, name="Beto Ruiz")

    edited = await client.put(
        f"{CUSTOMERS_URL}/{ana}",
        json={"name": "Ana Torres Vega", "phone": "987654321", "notes": "Sin cebolla"},
        headers=authorization_for(restaurant_a.admin),
    )
    everyone = await client.get(CUSTOMERS_URL, headers=authorization_for(restaurant_a.waiter))

    assert edited.status_code == 200, edited.text
    assert edited.json()["name"] == "Ana Torres Vega"
    assert edited.json()["notes"] == "Sin cebolla"
    assert everyone.json()["total"] == 2
    assert [c["name"] for c in everyone.json()["items"]] == ["Ana Torres Vega", "Beto Ruiz"]


async def test_edit_with_invalid_data_or_taken_phone_is_not_saved(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _create(client, restaurant_a, phone="987 654 321")
    beto = await _create(client, restaurant_a, name="Beto Ruiz")
    admin = authorization_for(restaurant_a.admin)

    conflicting = await client.put(
        f"{CUSTOMERS_URL}/{beto}", json={"name": "Beto", "phone": "987654321"}, headers=admin
    )
    invalid = await client.put(
        f"{CUSTOMERS_URL}/{beto}", json={"name": "Beto", "email": "sin-arroba"}, headers=admin
    )
    card = await client.get(f"{CUSTOMERS_URL}/{beto}", headers=admin)

    assert conflicting.status_code == 409
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "El correo no es válido."
    assert card.json()["name"] == "Beto Ruiz"
    assert card.json()["phone"] == ""


async def test_restaurant_cannot_edit_another_restaurants_customers(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    ana = await _create(client, restaurant_a)

    response = await client.put(
        f"{CUSTOMERS_URL}/{ana}",
        json={"name": "Robado"},
        headers=authorization_for(restaurant_b.admin),
    )
    card = await client.get(f"{CUSTOMERS_URL}/{ana}", headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 404
    assert card.json()["name"] == "Ana Torres"


async def test_without_consent_not_saved_and_with_consent_stored_in_card(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    without_consent = await client.post(CUSTOMERS_URL, json={"name": "Ana"}, headers=admin)
    with_consent = await client.post(
        CUSTOMERS_URL, json={"name": "Ana", "consent": True}, headers=admin
    )
    everyone = await client.get(CUSTOMERS_URL, headers=admin)

    assert without_consent.status_code == 422
    assert "datos personales" in without_consent.json()["detail"]
    assert with_consent.status_code == 201
    assert with_consent.json()["consent_at"] is not None
    assert with_consent.json()["consent_version"]
    assert everyone.json()["total"] == 1


async def test_save_customer_rejects_duplicate_phone_when_the_check_races(
    session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    """Dos altas a la vez pasan la revisión de `SaveCustomer`; el índice único frena la segunda."""
    customers = SqlAlchemyCustomerRepository(session)
    ana = await customers.add(
        Customer(restaurant_id=restaurant_a.id, name="Ana", phone="987654321")
    )
    beto = await customers.add(Customer(restaurant_id=restaurant_a.id, name="Beto"))

    with pytest.raises(PhoneTakenError) as added:
        await customers.add(Customer(restaurant_id=restaurant_a.id, name="Ana", phone="987654321"))
    with pytest.raises(PhoneTakenError):
        await customers.save(replace(beto, phone="987654321"))

    assert added.value.name == "Ana"
    same_phone = await session.scalar(
        select(func.count())
        .select_from(CustomerRow)
        .where(CustomerRow.phone_key == get_cipher().blind_index("987654321"))
    )
    assert same_phone == 1
    found = await customers.find_by_phone(restaurant_a.id, "987654321")
    assert found is not None
    assert found.id == ana.id


async def test_customers_without_phone_do_not_collide(
    session: AsyncSession, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    customers = SqlAlchemyCustomerRepository(session)

    await customers.add(Customer(restaurant_id=restaurant_a.id, name="Sin teléfono"))
    await customers.add(Customer(restaurant_id=restaurant_a.id, name="Otro sin teléfono"))
    # El mismo teléfono en otro local es otra ficha.
    await customers.add(Customer(restaurant_id=restaurant_a.id, name="Ana", phone="987654321"))
    await customers.add(Customer(restaurant_id=restaurant_b.id, name="Ana", phone="987654321"))

    assert await session.scalar(select(func.count()).select_from(CustomerRow)) == 4
