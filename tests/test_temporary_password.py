"""Contraseña temporal (RFS-20): quien recibe una contraseña ajena solo puede cambiarla."""

from __future__ import annotations

from dataclasses import replace

import pytest
from httpx import AsyncClient

from resthub.core.auth import ensure_password_is_own
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError
from resthub.core.identity import Principal
from tests.conftest import VALID_PASSWORD, StaffedRestaurant, authorization_for, url

STAFF_URL = url("staff")
LOGIN_URL = url("auth/login")
ME_URL = url("auth/me")
OWN_PASSWORD_URL = url("auth/me/password")
LOGOUT_URL = url("auth/logout")
OWN_PASSWORD = "clave-elegida-por-mi"


async def _login(client: AsyncClient, email: str, password: str = VALID_PASSWORD) -> dict[str, str]:
    response = await client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _register(client: AsyncClient, restaurant: StaffedRestaurant, email: str) -> None:
    response = await client.post(
        STAFF_URL,
        json={
            "email": email,
            "full_name": "Carla Ríos",
            "role_id": restaurant.waiter.role.id,
            "password": VALID_PASSWORD,
        },
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 201, response.text


async def test_new_staff_account_must_change_its_password_first(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _register(client, restaurant_a, "carla@local-a.pe")

    login = await client.post(
        LOGIN_URL, json={"email": "carla@local-a.pe", "password": VALID_PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    blocked = await client.get(url("orders"), headers=headers)
    me = await client.get(ME_URL, headers=headers)

    assert login.json()["must_change_password"] is True
    assert blocked.status_code == 403
    assert blocked.json()["code"] == ErrorCode.PASSWORD_CHANGE_REQUIRED.value
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True


async def test_changing_the_temporary_password_unlocks_the_account(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _register(client, restaurant_a, "carla@local-a.pe")
    headers = await _login(client, "carla@local-a.pe")

    changed = await client.post(
        OWN_PASSWORD_URL,
        json={"current_password": VALID_PASSWORD, "new_password": OWN_PASSWORD},
        headers=headers,
    )
    fresh = {"Authorization": f"Bearer {changed.json()['access_token']}"}
    orders = await client.get(url("orders"), headers=fresh)
    again = await client.post(
        LOGIN_URL, json={"email": "carla@local-a.pe", "password": OWN_PASSWORD}
    )

    assert changed.status_code == 200
    assert changed.json()["must_change_password"] is False
    assert orders.status_code == 200
    assert again.json()["must_change_password"] is False


async def test_temporary_password_still_allows_logout(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _register(client, restaurant_a, "carla@local-a.pe")
    headers = await _login(client, "carla@local-a.pe")

    response = await client.post(LOGOUT_URL, headers=headers)

    assert response.status_code == 204


async def test_reset_by_the_manager_is_temporary(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    reset = await client.post(
        f"{STAFF_URL}/{restaurant_a.waiter.id}/password",
        json={"new_password": "dictada-por-rosa"},
        headers=authorization_for(restaurant_a.admin),
    )
    headers = await _login(client, "mesero@local-a.pe", "dictada-por-rosa")

    blocked = await client.get(url("tables"), headers=headers)

    assert reset.status_code == 204
    assert blocked.status_code == 403
    assert blocked.json()["code"] == ErrorCode.PASSWORD_CHANGE_REQUIRED.value


async def test_accounts_that_chose_their_password_are_not_affected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    login = await client.post(
        LOGIN_URL, json={"email": "mesero@local-a.pe", "password": VALID_PASSWORD}
    )

    assert login.json()["must_change_password"] is False


def test_preview_session_is_never_asked_to_change_the_password() -> None:
    temporary = Principal(
        user_id=1, role_id=1, is_active=True, restaurant_id=1, must_change_password=True
    )

    assert ensure_password_is_own(replace(temporary, preview=True)).preview
    with pytest.raises(ApiError) as error:
        ensure_password_is_own(temporary)
    assert error.value.status_code == 403
