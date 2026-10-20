"""Una sesión nueva al entrar, al confirmar la contraseña y al desbloquear (ASVS V7.2.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.session_cookie import CSRF_COOKIE, PLATFORM_COOKIE, STAFF_COOKIE
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import (
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    url,
)

LOGIN_URL = url("auth/login")
ME_URL = url("auth/me")
REAUTHENTICATE_URL = url("auth/me/reauthenticate")
LOCK_URL = url("auth/me/lock")
UNLOCK_URL = url("auth/me/unlock")
PIN_URL = url("auth/me/pin")
ADMIN_EMAIL = "plataforma@resthub.dev"


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Un navegador por https: guarda las cookies `Secure` y las manda solo."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://api.test") as http:
        yield http


@pytest.fixture
async def other_browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://api.test") as http:
        yield http


async def _login(http: AsyncClient, email: str) -> dict[str, str]:
    response = await http.post(LOGIN_URL, json={"email": email, "password": VALID_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_login_closes_the_session_the_browser_already_had(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    first = await _login(browser, restaurant_a.waiter.email)

    second = await _login(browser, restaurant_a.admin.email)

    assert (await browser.get(ME_URL, headers=first)).status_code == 401
    me = await browser.get(ME_URL, headers=second)
    assert me.status_code == 200
    assert me.json()["user"]["id"] == restaurant_a.admin.id


async def test_login_in_another_browser_keeps_the_first_session(
    browser: AsyncClient, other_browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    first = await _login(browser, restaurant_a.waiter.email)

    await _login(other_browser, restaurant_a.waiter.email)

    assert (await browser.get(ME_URL, headers=first)).status_code == 200


async def test_login_ignores_a_cookie_this_server_did_not_sign(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    browser.cookies.set(STAFF_COOKIE, "no-es-un-token", domain="api.test")
    browser.cookies.set(PLATFORM_COOKIE, "tampoco", domain="api.test")

    headers = await _login(browser, restaurant_a.waiter.email)

    assert (await browser.get(ME_URL, headers=headers)).status_code == 200


async def test_reauthenticate_without_rotation_keeps_the_token(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(browser, restaurant_a.waiter.email)

    response = await browser.post(
        REAUTHENTICATE_URL, json={"password": VALID_PASSWORD}, headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] is None
    assert body["csrf_token"] is None
    assert (await browser.get(ME_URL, headers=headers)).status_code == 200


async def test_reauthenticate_with_rotation_issues_a_new_session_id(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(browser, restaurant_a.waiter.email)
    old_sid = TEST_TOKEN_SERVICE.decode(headers["Authorization"].removeprefix("Bearer ")).session_id

    response = await browser.post(
        REAUTHENTICATE_URL,
        json={"password": VALID_PASSWORD, "rotate_session": True},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    new_token = body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["csrf_token"] == browser.cookies[CSRF_COOKIE]
    assert browser.cookies[STAFF_COOKIE] == new_token
    assert TEST_TOKEN_SERVICE.decode(new_token).session_id != old_sid
    assert (await browser.get(ME_URL, headers=headers)).status_code == 401
    # La contraseña confirmada pasa a la sesión nueva: la acción crítica sigue.
    pin = await browser.put(PIN_URL, json={"pin": "1234"}, headers=_bearer(new_token))
    assert pin.status_code == 204, pin.text


async def test_platform_reauthenticate_with_rotation_issues_a_platform_token(
    browser: AsyncClient, session: AsyncSession
) -> None:
    await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email=ADMIN_EMAIL,
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    headers = await _login(browser, ADMIN_EMAIL)

    response = await browser.post(
        REAUTHENTICATE_URL,
        json={"password": VALID_PASSWORD, "rotate_session": True},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    new_token = response.json()["access_token"]
    assert browser.cookies[PLATFORM_COOKIE] == new_token
    assert (await browser.get(ME_URL, headers=headers)).status_code == 401
    me = await browser.get(ME_URL, headers=_bearer(new_token))
    assert me.status_code == 200
    assert me.json()["kind"] == "platform"


async def test_unlock_without_rotation_answers_no_content(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(browser, restaurant_a.waiter.email)
    assert (await browser.post(LOCK_URL, headers=headers)).status_code == 204

    response = await browser.post(UNLOCK_URL, json={"password": VALID_PASSWORD}, headers=headers)

    assert response.status_code == 204
    me = await browser.get(ME_URL, headers=headers)
    assert me.json()["session_locked"] is False


async def test_unlock_with_rotation_answers_a_new_token(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(browser, restaurant_a.waiter.email)
    assert (await browser.post(LOCK_URL, headers=headers)).status_code == 204

    response = await browser.post(
        UNLOCK_URL,
        json={"password": VALID_PASSWORD, "rotate_session": True},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "staff"
    assert body["session_locked"] is False
    assert body["csrf_token"] == browser.cookies[CSRF_COOKIE]
    assert (await browser.get(ME_URL, headers=headers)).status_code == 401
    me = await browser.get(ME_URL, headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["session_locked"] is False


async def test_unlock_with_rotation_of_an_unlocked_session_keeps_the_token(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(browser, restaurant_a.waiter.email)

    response = await browser.post(
        UNLOCK_URL,
        json={"password": VALID_PASSWORD, "rotate_session": True},
        headers=headers,
    )

    assert response.status_code == 204
    assert (await browser.get(ME_URL, headers=headers)).status_code == 200
