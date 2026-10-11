"""Un número fuera de lo que la base representa es un 422, nunca un 500.

Hallado por Schemathesis: un identificador de la ruta o un `offset` por encima del entero de la
base llegaba a la consulta y el driver fallaba (`OverflowError` en SQLite, `DataError` en
PostgreSQL), que el cliente veía como «Internal Server Error».
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from resthub.core.api_params import MAX_ID
from resthub.core.pagination import MAX_OFFSET
from tests.conftest import StaffedRestaurant, authorization_for, url

TOO_BIG = MAX_ID + 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"orders/{TOO_BIG}"),
        ("POST", f"orders/{TOO_BIG}/send"),
        ("POST", f"orders/{TOO_BIG * 10**20}/cancel"),
        ("POST", f"inventory/purchase-orders/{TOO_BIG}/send"),
        ("GET", f"staff/{TOO_BIG}"),
        ("GET", "orders/0"),
    ],
)
async def test_path_identifier_out_of_range_is_unprocessable(
    client: AsyncClient, restaurant_a: StaffedRestaurant, method: str, path: str
) -> None:
    response = await client.request(
        method, url(path), headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    ["staff", "customers", "billing/invoices", "orders", "activity", "inventory/movements"],
)
async def test_offset_past_the_limit_is_unprocessable(
    client: AsyncClient, restaurant_a: StaffedRestaurant, path: str
) -> None:
    headers = authorization_for(restaurant_a.admin)

    too_far = await client.get(url(path), params={"offset": MAX_OFFSET + 1}, headers=headers)
    at_the_limit = await client.get(url(path), params={"offset": MAX_OFFSET}, headers=headers)

    assert too_far.status_code == 422
    assert at_the_limit.status_code in {200, 422}
    assert at_the_limit.status_code < 500


async def test_listed_identifiers_out_of_range_are_unprocessable(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)

    notes = await client.get(
        url("insights/order-notes"), params={"order_ids": [1, TOO_BIG]}, headers=headers
    )
    staff = await client.get(url("staff"), params={"role_id": TOO_BIG}, headers=headers)

    assert notes.status_code == 422
    assert staff.status_code == 422


async def test_body_identifier_out_of_range_is_unprocessable(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)

    order = await client.post(
        url("orders"),
        json={
            "type": "takeaway",
            "customer_name": "Ana",
            "items": [{"menu_item_id": TOO_BIG * 10**10, "quantity": 1}],
        },
        headers=headers,
    )
    reorder = await client.put(url("tables/order"), json={"ids": [1, TOO_BIG]}, headers=headers)

    assert order.status_code == 422
    assert reorder.status_code == 422
