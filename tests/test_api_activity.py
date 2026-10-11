"""La bitácora por HTTP: quién la ve y de qué restaurante."""

from __future__ import annotations

from httpx import AsyncClient

from resthub.core.activity import ActivityKind
from tests.conftest import VALID_PASSWORD, StaffedRestaurant, authorization_for, url

URL = url("activity")
LOGIN_URL = url("auth/login")


async def _sign_in(client: AsyncClient, email: str) -> None:
    response = await client.post(LOGIN_URL, json={"email": email, "password": VALID_PASSWORD})
    assert response.status_code == 200


async def test_owner_sees_only_movements_of_own_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    await _sign_in(client, "mesero@local-a.pe")
    await _sign_in(client, "mesero@local-b.pe")
    await _sign_in(client, "encargado@local-b.pe")

    response = await client.get(URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    entry = body["items"][0]
    assert entry["kind"] == ActivityKind.SIGNED_IN.value
    assert entry["kind_label"] == "Inició sesión"
    assert (entry["user_id"], entry["user_name"]) == (restaurant_a.waiter.id, "Luis Torres")
    assert entry["user_role_id"] == restaurant_a.waiter.role.id
    assert entry["user_role_label"] == "Mesero"


async def test_activity_log_filters_by_role(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _sign_in(client, "mesero@local-a.pe")
    await _sign_in(client, "encargado@local-a.pe")

    response = await client.get(
        URL,
        params={"role_id": restaurant_a.admin.role.id},
        headers=authorization_for(restaurant_a.admin),
    )

    assert [item["user_id"] for item in response.json()["items"]] == [restaurant_a.admin.id]


async def test_waiter_does_not_see_activity_log(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(URL, headers=authorization_for(restaurant_a.waiter))

    assert response.status_code == 403
