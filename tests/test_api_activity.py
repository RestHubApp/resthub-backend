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


LOGOUT_URL = url("auth/logout")
ORDERS_URL = url("orders")


async def _kinds(client: AsyncClient, headers: dict[str, str], **params: object) -> list[str]:
    response = await client.get(URL, params=params, headers=headers)  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    return [item["kind"] for item in response.json()["items"]]


async def test_logout_responds_204_and_logs_the_exit(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _sign_in(client, "mesero@local-a.pe")

    response = await client.post(LOGOUT_URL, headers=authorization_for(restaurant_a.waiter))
    log = await client.get(URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 204
    assert response.content == b""
    newest = log.json()["items"][0]
    assert newest["kind"] == ActivityKind.SIGNED_OUT.value
    assert newest["kind_label"] == "Cerró sesión"
    assert newest["user_id"] == restaurant_a.waiter.id


async def test_logout_requires_authentication(client: AsyncClient) -> None:
    assert (await client.post(LOGOUT_URL)).status_code == 401


async def test_filter_by_person_returns_only_their_movements(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _sign_in(client, "mesero@local-a.pe")
    await _sign_in(client, "encargado@local-a.pe")
    admin = authorization_for(restaurant_a.admin)

    response = await client.get(URL, params={"user_id": restaurant_a.waiter.id}, headers=admin)
    nobody = await client.get(URL, params={"user_id": 999_999}, headers=admin)

    assert [item["user_id"] for item in response.json()["items"]] == [restaurant_a.waiter.id]
    assert nobody.json() == {"items": [], "total": 0}


async def test_person_and_role_filters_are_intersected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _sign_in(client, "mesero@local-a.pe")
    admin = authorization_for(restaurant_a.admin)

    same_role = await client.get(
        URL,
        params={"user_id": restaurant_a.waiter.id, "role_id": restaurant_a.waiter.role.id},
        headers=admin,
    )
    other_role = await client.get(
        URL,
        params={"user_id": restaurant_a.waiter.id, "role_id": restaurant_a.admin.role.id},
        headers=admin,
    )

    assert same_role.json()["total"] == 1
    assert other_role.json()["total"] == 0


async def test_filter_by_group_sessions_and_orders(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _sign_in(client, "mesero@local-a.pe")
    waiter = authorization_for(restaurant_a.waiter)
    admin = authorization_for(restaurant_a.admin)
    opened = await client.post(ORDERS_URL, json={"type": "takeaway", "items": []}, headers=waiter)
    assert opened.status_code == 201, opened.text
    await client.post(LOGOUT_URL, headers=waiter)

    sessions = await _kinds(client, admin, group="sessions")
    orders = await _kinds(client, admin, group="orders")
    both = await _kinds(client, admin, group=["sessions", "orders"])

    assert sorted(sessions) == [ActivityKind.SIGNED_IN.value, ActivityKind.SIGNED_OUT.value]
    assert orders == [ActivityKind.ORDER_CREATED.value]
    assert sorted(both) == [
        ActivityKind.ORDER_CREATED.value,
        ActivityKind.SIGNED_IN.value,
        ActivityKind.SIGNED_OUT.value,
    ]


async def test_group_and_kind_add_up_and_kind_alone_keeps_working(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _sign_in(client, "mesero@local-a.pe")
    waiter = authorization_for(restaurant_a.waiter)
    admin = authorization_for(restaurant_a.admin)
    await client.post(ORDERS_URL, json={"type": "takeaway", "items": []}, headers=waiter)

    only_kind = await _kinds(client, admin, kind=ActivityKind.SIGNED_IN.value)
    combined = await _kinds(client, admin, kind=ActivityKind.SIGNED_IN.value, group="orders")

    assert only_kind == [ActivityKind.SIGNED_IN.value]
    assert sorted(combined) == [ActivityKind.ORDER_CREATED.value, ActivityKind.SIGNED_IN.value]


async def test_unknown_group_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(
        URL, params={"group": "nope"}, headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 422
