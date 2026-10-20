"""Las búsquedas por texto no dejan pasar comodines de `LIKE` (`%`, `_`, barra invertida)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from resthub.core.sql_like import contains_pattern, escape_like
from tests.conftest import StaffedRestaurant, authorization_for, url

CUSTOMERS_URL = url("customers")
STAFF_URL = url("staff")


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        ("ana", "ana"),
        ("50%", "50\\%"),
        ("a_b", "a\\_b"),
        ("c:\\temp", "c:\\\\temp"),
        ("\\%", "\\\\\\%"),
    ],
)
def test_escape_like_neutralizes_wildcards_and_the_escape_itself(text: str, escaped: str) -> None:
    assert escape_like(text) == escaped
    assert contains_pattern(text) == f"%{escaped}%"


async def _create_customer(
    client: AsyncClient, restaurant: StaffedRestaurant, name: str, phone: str
) -> None:
    response = await client.post(
        CUSTOMERS_URL,
        json={"name": name, "phone": phone, "consent": True},
        headers=authorization_for(restaurant.waiter),
    )
    assert response.status_code == 201, response.text


async def _customer_names(client: AsyncClient, restaurant: StaffedRestaurant, q: str) -> list[str]:
    response = await client.get(
        CUSTOMERS_URL, params={"q": q}, headers=authorization_for(restaurant.waiter)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return sorted(item["name"] for item in items)


async def test_customer_search_takes_percent_and_underscore_literally(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _create_customer(client, restaurant_a, "Ana Torres", "987654321")
    await _create_customer(client, restaurant_a, "Promo 50% Luis", "987654322")
    await _create_customer(client, restaurant_a, "Rosa_B", "987654323")
    await _create_customer(client, restaurant_a, "Barra\\Uno", "987654324")

    assert await _customer_names(client, restaurant_a, "%") == ["Promo 50% Luis"]
    assert await _customer_names(client, restaurant_a, "50%") == ["Promo 50% Luis"]
    assert await _customer_names(client, restaurant_a, "_") == ["Rosa_B"]
    assert await _customer_names(client, restaurant_a, "a_b") == ["Rosa_B"]
    assert await _customer_names(client, restaurant_a, "\\") == ["Barra\\Uno"]
    # Una búsqueda normal sigue encontrando por parte del nombre, sin importar mayúsculas.
    assert await _customer_names(client, restaurant_a, "torres") == ["Ana Torres"]


async def test_staff_search_takes_percent_and_underscore_literally(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)

    async def found(search: str) -> list[int]:
        response = await client.get(STAFF_URL, params={"search": search}, headers=headers)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["items"]]

    # Sin escapar, `%` y `_` calzaban con todo el personal.
    assert await found("%") == []
    assert await found("_") == []
    assert await found("\\") == []
    assert await found("mesero") == [restaurant_a.waiter.id]
