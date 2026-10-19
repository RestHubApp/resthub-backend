"""La administración del sistema no usa la plataforma sin verificación en dos pasos (RFS-22)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.two_factor import get_platform_two_factor_policy
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import TEST_HASHER, VALID_PASSWORD, FakeClock, url
from tests.test_two_factor import ADMIN_EMAIL, TWO_FACTOR_URL, _code, _enable, _headers

RESTAURANTS_URL = url("platform/restaurants")


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


@pytest.fixture
def required(app: FastAPI) -> None:
    app.dependency_overrides[get_platform_two_factor_policy] = lambda: True


async def test_without_it_the_platform_answers_403_and_me_says_why(
    client: AsyncClient, required: None, platform_admin: PlatformAdmin
) -> None:
    headers = await _headers(client, ADMIN_EMAIL)

    restaurants = await client.get(RESTAURANTS_URL, headers=headers)
    me = await client.get(url("auth/me"), headers=headers)
    status = await client.get(TWO_FACTOR_URL, headers=headers)

    assert (restaurants.status_code, restaurants.json()["code"]) == (
        403,
        "two_factor_setup_required",
    )
    assert me.json()["two_factor_setup_required"] is True
    # Lo que hace falta para activarla sigue abierto.
    assert status.status_code == 200


async def test_once_enabled_the_platform_opens_and_cannot_be_turned_off(
    client: AsyncClient, clock: FakeClock, required: None, platform_admin: PlatformAdmin
) -> None:
    secret, _ = await _enable(client, clock, ADMIN_EMAIL)
    login = await client.post(
        url("auth/login"),
        json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD, "otp_code": _code(secret, clock)},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    restaurants = await client.get(RESTAURANTS_URL, headers=headers)
    me = await client.get(url("auth/me"), headers=headers)
    disabled = await client.delete(TWO_FACTOR_URL, headers=headers)

    assert restaurants.status_code == 200
    assert me.json()["two_factor_setup_required"] is False
    assert (disabled.status_code, disabled.json()["code"]) == (409, "two_factor_state_conflict")


async def test_without_the_requirement_nothing_changes(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    headers = await _headers(client, ADMIN_EMAIL)

    restaurants = await client.get(RESTAURANTS_URL, headers=headers)
    me = await client.get(url("auth/me"), headers=headers)

    assert restaurants.status_code == 200
    assert me.json()["two_factor_setup_required"] is False
