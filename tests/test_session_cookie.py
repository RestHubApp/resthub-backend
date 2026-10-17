"""La sesión en cookie `HttpOnly` con doble envío contra CSRF (S2)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.error_codes import ErrorCode
from resthub.core.session_cookie import CSRF_COOKIE, CSRF_HEADER, PLATFORM_COOKIE, STAFF_COOKIE
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import TEST_HASHER, VALID_PASSWORD, StaffedRestaurant, url

LOGIN_URL = url("auth/login")


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Un navegador por https: guarda las cookies `Secure` y las manda solo."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://api.test") as client:
        yield client


async def _login(browser: AsyncClient, email: str) -> str:
    response = await browser.post(LOGIN_URL, json={"email": email, "password": VALID_PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


async def test_login_sets_an_httponly_secure_cookie(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await browser.post(
        LOGIN_URL, json={"email": restaurant_a.waiter.email, "password": VALID_PASSWORD}
    )

    cookies = {
        header.split("=", 1)[0]: header.lower()
        for header in response.headers.get_list("set-cookie")
    }
    assert {STAFF_COOKIE, CSRF_COOKIE} <= cookies.keys()
    for name in (STAFF_COOKIE, CSRF_COOKIE):
        assert "httponly" in cookies[name]
        assert "secure" in cookies[name]
        assert "samesite=none" in cookies[name]
    assert response.json()["csrf_token"] == browser.cookies[CSRF_COOKIE]


async def test_cookie_alone_is_enough_to_read(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _login(browser, restaurant_a.waiter.email)

    me = await browser.get(url("auth/me"))
    tables = await browser.get(url("tables"))

    assert me.status_code == 200
    assert me.json()["user"]["id"] == restaurant_a.waiter.id
    assert tables.status_code == 200


async def test_changes_with_the_cookie_require_the_csrf_header(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    csrf = await _login(browser, restaurant_a.admin.email)
    body = {"label": "Terraza 1"}

    missing = await browser.post(url("tables"), json=body)
    wrong = await browser.post(url("tables"), json=body, headers={CSRF_HEADER: "otro-valor"})
    right = await browser.post(url("tables"), json=body, headers={CSRF_HEADER: csrf})

    assert missing.status_code == 403
    assert missing.json()["code"] == ErrorCode.CSRF_TOKEN_INVALID.value
    assert wrong.status_code == 403
    assert right.status_code == 201, right.text


async def test_bearer_header_does_not_need_csrf(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    login = await browser.post(
        LOGIN_URL, json={"email": restaurant_a.admin.email, "password": VALID_PASSWORD}
    )
    browser.cookies.clear()

    response = await browser.post(
        url("tables"),
        json={"label": "Barra 2"},
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 201


async def test_logout_clears_the_cookies(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    csrf = await _login(browser, restaurant_a.waiter.email)

    response = await browser.post(url("auth/logout"), headers={CSRF_HEADER: csrf})
    after = await browser.get(url("auth/me"))

    assert response.status_code == 204
    assert STAFF_COOKIE not in browser.cookies
    assert after.status_code == 401


async def test_refresh_renews_the_cookie_and_the_csrf_value(
    browser: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    csrf = await _login(browser, restaurant_a.waiter.email)

    response = await browser.post(url("auth/refresh"), headers={CSRF_HEADER: csrf})

    assert response.status_code == 200
    assert response.json()["csrf_token"] not in {None, csrf}
    assert browser.cookies[CSRF_COOKIE] == response.json()["csrf_token"]


async def test_platform_session_uses_its_own_cookie_and_replaces_the_staff_one(
    browser: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    await _login(browser, restaurant_a.waiter.email)

    await _login(browser, "equipo@resthub.dev")
    me = await browser.get(url("auth/me"))
    restaurants = await browser.get(url("platform/restaurants"))
    staff_only = await browser.get(url("tables"))

    assert STAFF_COOKIE not in browser.cookies
    assert PLATFORM_COOKIE in browser.cookies
    assert me.json()["kind"] == "platform"
    assert restaurants.status_code == 200
    # La cookie de plataforma no vale en el restaurante.
    assert staff_only.status_code == 401
