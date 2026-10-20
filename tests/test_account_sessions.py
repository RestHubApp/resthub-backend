"""Sesiones por dispositivo (RFS-17) y contraseña confirmada para acciones críticas (RFS-21)."""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.clock import SystemClock
from resthub.core.permissions import Permission
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import (
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    FakeClock,
    StaffedRestaurant,
    authorization_for,
    registered_claims,
    url,
)

LOGIN_URL = url("auth/login")
LOGOUT_URL = url("auth/logout")
REFRESH_URL = url("auth/refresh")
ME_URL = url("auth/me")
SESSIONS_URL = url("auth/me/sessions")
REAUTHENTICATE_URL = url("auth/me/reauthenticate")
ANDROID_CHROME = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/129.0 Mobile Safari"
WINDOWS_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
WAITER_EMAIL = "mesero@local-a.pe"
OWNER_EMAIL = "encargado@local-a.pe"
ADMIN_EMAIL = "plataforma@resthub.dev"


async def _login(client: AsyncClient, email: str, agent: str = ANDROID_CHROME) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL,
        json={"email": email, "password": VALID_PASSWORD},
        headers={"User-Agent": agent},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _sessions(client: AsyncClient, headers: dict[str, str]) -> list[dict[str, object]]:
    response = await client.get(SESSIONS_URL, headers=headers)
    assert response.status_code == 200, response.text
    return list(response.json())


async def _status(client: AsyncClient, headers: dict[str, str]) -> int:
    return (await client.get(ME_URL, headers=headers)).status_code


async def _reauthenticate(client: AsyncClient, headers: dict[str, str]) -> None:
    """Cerrar otras sesiones pide la contraseña confirmada hace poco (ASVS V7.5.2)."""
    response = await client.post(
        REAUTHENTICATE_URL, json={"password": VALID_PASSWORD}, headers=headers
    )
    assert response.status_code == 200, response.text


@pytest.fixture
async def platform_admin(session: AsyncSession) -> PlatformAdmin:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email=ADMIN_EMAIL,
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    return admin


async def test_login_opens_a_session_for_the_device(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client, WAITER_EMAIL)

    sessions = await _sessions(client, headers)

    assert len(sessions) == 1
    assert sessions[0]["device"] == "Chrome en Android"
    assert sessions[0]["current"] is True
    # El nombre público no es el identificador que viaja en el token.
    claims = TEST_TOKEN_SERVICE.decode(headers["Authorization"].removeprefix("Bearer "))
    assert sessions[0]["id"] != claims.session_id


async def test_a_token_without_session_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    now = SystemClock().now()
    token = jwt.encode(
        {
            "sub": str(restaurant_a.waiter.id),
            "scope": "restaurant",
            "restaurant_id": restaurant_a.id,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            **registered_claims(),
        },
        "secreto-de-prueba-con-largo-suficiente",
        algorithm="HS256",
    )

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "La sesión se cerró: vuelve a iniciar sesión."


async def test_logout_closes_only_this_device(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    phone = await _login(client, WAITER_EMAIL)
    laptop = await _login(client, WAITER_EMAIL, WINDOWS_FIREFOX)

    assert (await client.post(LOGOUT_URL, headers=phone)).status_code == 204

    assert await _status(client, phone) == 401
    assert await _status(client, laptop) == 200
    assert [entry["device"] for entry in await _sessions(client, laptop)] == ["Firefox en Windows"]


async def test_closing_the_session_of_another_device(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    lost_phone = await _login(client, WAITER_EMAIL)
    laptop = await _login(client, WAITER_EMAIL, WINDOWS_FIREFOX)
    phone_ref = next(
        entry["id"] for entry in await _sessions(client, laptop) if not entry["current"]
    )

    await _reauthenticate(client, laptop)
    response = await client.delete(f"{SESSIONS_URL}/{phone_ref}", headers=laptop)

    assert response.status_code == 204
    assert await _status(client, lost_phone) == 401
    assert await _status(client, laptop) == 200
    activity = await client.get(url("activity"), headers=authorization_for(restaurant_a.admin))
    assert ActivityKind.SESSION_CLOSED.value in {item["kind"] for item in activity.json()["items"]}


async def test_closing_the_current_session_is_signing_out(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client, WAITER_EMAIL)
    own_ref = (await _sessions(client, headers))[0]["id"]

    assert (await client.delete(f"{SESSIONS_URL}/{own_ref}", headers=headers)).status_code == 204
    assert await _status(client, headers) == 401


async def test_close_every_other_session(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    first = await _login(client, WAITER_EMAIL)
    second = await _login(client, WAITER_EMAIL, WINDOWS_FIREFOX)
    kept = await _login(client, WAITER_EMAIL)
    await _reauthenticate(client, kept)

    assert (await client.delete(SESSIONS_URL, headers=kept)).status_code == 204

    assert (await _status(client, first), await _status(client, second)) == (401, 401)
    assert len(await _sessions(client, kept)) == 1


async def test_closing_other_sessions_asks_for_the_password_again(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    phone = await _login(client, WAITER_EMAIL)
    laptop = await _login(client, WAITER_EMAIL, WINDOWS_FIREFOX)
    phone_ref = next(
        entry["id"] for entry in await _sessions(client, laptop) if not entry["current"]
    )

    one = await client.delete(f"{SESSIONS_URL}/{phone_ref}", headers=laptop)
    others = await client.delete(SESSIONS_URL, headers=laptop)
    await _reauthenticate(client, laptop)
    clock.advance(301)
    expired = await client.delete(SESSIONS_URL, headers=laptop)

    for response in (one, others, expired):
        assert response.status_code == 403
        assert response.json()["code"] == "reauthentication_required"
    assert await _status(client, phone) == 200


async def test_closing_the_current_session_needs_no_password(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client, WAITER_EMAIL)
    own_ref = (await _sessions(client, headers))[0]["id"]

    response = await client.delete(f"{SESSIONS_URL}/{own_ref}", headers=headers)

    assert response.status_code == 204
    assert await _status(client, headers) == 401


async def test_an_unknown_session_is_404(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    other = await _login(client, "mesero@local-b.pe")
    other_ref = (await _sessions(client, other))[0]["id"]
    headers = await _login(client, WAITER_EMAIL)

    foreign = await client.delete(f"{SESSIONS_URL}/{other_ref}", headers=headers)
    missing = await client.delete(f"{SESSIONS_URL}/abc123", headers=headers)

    assert (foreign.status_code, missing.status_code) == (404, 404)
    assert foreign.json()["code"] == "session_not_found"
    assert await _status(client, other) == 200


async def test_refresh_keeps_the_device_session(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client, WAITER_EMAIL)

    refreshed = await client.post(REFRESH_URL, headers=headers)

    old = TEST_TOKEN_SERVICE.decode(headers["Authorization"].removeprefix("Bearer "))
    new = TEST_TOKEN_SERVICE.decode(refreshed.json()["access_token"])
    assert new.session_id == old.session_id
    assert len(await _sessions(client, headers)) == 1


async def test_a_session_unused_longer_than_a_token_is_closed(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client, WAITER_EMAIL)

    clock.advance(3601)

    assert await _status(client, headers) == 401


async def test_changing_the_password_closes_the_other_devices(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _login(client, WAITER_EMAIL, WINDOWS_FIREFOX)
    phone = await _login(client, WAITER_EMAIL)

    changed = await client.post(
        url("auth/me/password"),
        json={"current_password": VALID_PASSWORD, "new_password": "otra-contrasena-larga"},
        headers=phone,
    )

    new_phone = {"Authorization": f"Bearer {changed.json()['access_token']}"}
    sessions = await _sessions(client, new_phone)
    assert [(entry["device"], entry["current"]) for entry in sessions] == [
        ("Chrome en Android", True)
    ]


async def test_owner_sees_and_closes_the_sessions_of_the_staff(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    waiter = await _login(client, WAITER_EMAIL)
    owner = authorization_for(restaurant_a.admin)
    staff_sessions = url(f"staff/{restaurant_a.waiter.id}/sessions")

    listed = await client.get(staff_sessions, headers=owner)
    ref = listed.json()[0]["id"]
    closed = await client.delete(f"{staff_sessions}/{ref}", headers=owner)

    assert [entry["device"] for entry in listed.json()] == ["Chrome en Android"]
    assert closed.status_code == 204
    assert await _status(client, waiter) == 401
    activity = await client.get(url("activity"), headers=owner)
    kinds = {item["kind"] for item in activity.json()["items"]}
    assert ActivityKind.STAFF_SESSION_CLOSED.value in kinds


async def test_owner_closes_every_session_of_a_member(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    phone = await _login(client, WAITER_EMAIL)
    laptop = await _login(client, WAITER_EMAIL, WINDOWS_FIREFOX)

    response = await client.delete(
        url(f"staff/{restaurant_a.waiter.id}/sessions"),
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 204
    assert (await _status(client, phone), await _status(client, laptop)) == (401, 401)


async def test_staff_sessions_stay_inside_the_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    foreign = url(f"staff/{restaurant_b.waiter.id}/sessions")
    own = url(f"staff/{restaurant_a.admin.id}/sessions")

    other_restaurant = await client.get(foreign, headers=authorization_for(restaurant_a.admin))
    close_foreign = await client.delete(foreign, headers=authorization_for(restaurant_a.admin))
    no_permission = await client.get(own, headers=authorization_for(restaurant_a.waiter))
    unknown_ref = await client.delete(
        f"{url(f'staff/{restaurant_a.waiter.id}/sessions')}/abc123",
        headers=authorization_for(restaurant_a.admin),
    )

    assert other_restaurant.status_code == 404
    assert close_foreign.status_code == 404
    assert no_permission.status_code == 403
    assert unknown_ref.status_code == 404


async def test_a_platform_account_has_its_own_sessions(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    first = await _login(client, ADMIN_EMAIL)
    second = await _login(client, ADMIN_EMAIL, WINDOWS_FIREFOX)
    first_ref = next(
        entry["id"] for entry in await _sessions(client, second) if not entry["current"]
    )

    await _reauthenticate(client, second)
    assert (await client.delete(f"{SESSIONS_URL}/{first_ref}", headers=second)).status_code == 204
    assert await _status(client, first) == 401
    assert (await client.get(url("platform/restaurants"), headers=first)).status_code == 401
    assert (await client.post(LOGOUT_URL, headers=second)).status_code == 204
    assert await _status(client, second) == 401


async def test_critical_actions_ask_for_the_password_again(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    owner = await _login(client, OWNER_EMAIL)
    new_role = {"name": "Cajero", "permissions": [Permission.ORDERS_CHARGE.value]}

    before = await client.post(url("roles"), json=new_role, headers=owner)
    wrong = await client.post(REAUTHENTICATE_URL, json={"password": "otra"}, headers=owner)
    confirmed = await client.post(
        REAUTHENTICATE_URL, json={"password": VALID_PASSWORD}, headers=owner
    )
    after = await client.post(url("roles"), json=new_role, headers=owner)
    clock.advance(301)
    expired = await client.post(url("roles"), json={**new_role, "name": "Otro"}, headers=owner)

    assert (before.status_code, before.json()["code"]) == (403, "reauthentication_required")
    assert (wrong.status_code, wrong.json()["code"]) == (400, "wrong_current_password")
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["valid_until"] > body["reauthenticated_at"]
    assert after.status_code == 201
    assert expired.json()["code"] == "reauthentication_required"


async def test_reauthentication_is_only_for_that_device(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    phone = await _login(client, OWNER_EMAIL)
    laptop = await _login(client, OWNER_EMAIL, WINDOWS_FIREFOX)
    await client.post(REAUTHENTICATE_URL, json={"password": VALID_PASSWORD}, headers=phone)

    response = await client.put(url("billing/settings"), json={}, headers=laptop)

    assert response.json()["code"] == "reauthentication_required"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "billing/settings", {}),
        ("put", "billing/settings/certificate", {}),
        ("delete", "roles/1", None),
    ],
)
async def test_each_critical_action_requires_it(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    owner = await _login(client, OWNER_EMAIL)

    response = await client.request(method, url(path), json=body, headers=owner)

    assert response.json()["code"] == "reauthentication_required"


async def test_deactivating_or_changing_a_role_asks_for_it_but_renaming_does_not(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    owner = await _login(client, OWNER_EMAIL)
    waiter = await _login(client, WAITER_EMAIL)
    member = url(f"staff/{restaurant_a.waiter.id}")

    renamed = await client.patch(member, json={"full_name": "Luis T."}, headers=owner)
    same_role = await client.patch(
        member, json={"role_id": restaurant_a.waiter.role.id}, headers=owner
    )
    promoted = await client.patch(
        member, json={"role_id": restaurant_a.admin.role.id}, headers=owner
    )
    deactivate = {"is_active": False, "reason": "Ya no trabaja en el local"}
    blocked = await client.patch(f"{member}/status", json=deactivate, headers=owner)
    await client.post(REAUTHENTICATE_URL, json={"password": VALID_PASSWORD}, headers=owner)
    deactivated = await client.patch(f"{member}/status", json=deactivate, headers=owner)

    assert (renamed.status_code, same_role.status_code) == (200, 200)
    assert promoted.json()["code"] == "reauthentication_required"
    assert blocked.json()["code"] == "reauthentication_required"
    assert deactivated.status_code == 200
    assert await _status(client, waiter) == 401
    owner_view = await client.get(url(f"staff/{restaurant_a.waiter.id}/sessions"), headers=owner)
    assert owner_view.json() == []


async def test_a_platform_account_confirms_before_deactivating_a_restaurant(
    client: AsyncClient, platform_admin: PlatformAdmin, restaurant_a: StaffedRestaurant
) -> None:
    admin = await _login(client, ADMIN_EMAIL)
    restaurant = url(f"platform/restaurants/{restaurant_a.id}")

    renamed = await client.patch(restaurant, json={"name": "Local A"}, headers=admin)
    blocked = await client.patch(restaurant, json={"is_active": False}, headers=admin)
    wrong = await client.post(REAUTHENTICATE_URL, json={"password": "otra"}, headers=admin)
    await client.post(REAUTHENTICATE_URL, json={"password": VALID_PASSWORD}, headers=admin)
    deactivated = await client.patch(restaurant, json={"is_active": False}, headers=admin)

    assert renamed.status_code == 200
    assert blocked.json()["code"] == "reauthentication_required"
    assert wrong.status_code == 400
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False


async def test_repeated_wrong_passwords_are_throttled(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client, OWNER_EMAIL)

    statuses = [
        (
            await client.post(REAUTHENTICATE_URL, json={"password": "otra"}, headers=headers)
        ).status_code
        for _ in range(8)
    ]

    assert 429 in statuses
