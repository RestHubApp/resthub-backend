"""Quien administra el personal de un local no trabaja sin verificación en dos pasos."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from resthub.core.two_factor import get_staff_two_factor_policy
from resthub.modules.accounts.domain.entities import PRIVACY_VERSION, TERMS_VERSION
from tests.conftest import VALID_PASSWORD, FakeClock, StaffedRestaurant, authorization_for, url
from tests.test_two_factor import OWNER_EMAIL, TWO_FACTOR_URL, _code, _enable, _headers

STAFF_URL = url("staff")


@pytest.fixture
def required(app: FastAPI) -> None:
    app.dependency_overrides[get_staff_two_factor_policy] = lambda: True


async def test_without_it_the_owner_answers_403_and_me_says_why(
    client: AsyncClient, required: None, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _headers(client, OWNER_EMAIL)

    staff = await client.get(STAFF_URL, headers=headers)
    me = await client.get(url("auth/me"), headers=headers)
    status = await client.get(TWO_FACTOR_URL, headers=headers)
    terms = await client.post(
        url("auth/me/terms"),
        json={"terms_version": TERMS_VERSION, "privacy_version": PRIVACY_VERSION},
        headers=headers,
    )

    assert (staff.status_code, staff.json()["code"]) == (403, "two_factor_setup_required")
    assert me.json()["two_factor_setup_required"] is True
    # Lo que hace falta para activarla sigue abierto, y los términos se aceptan antes.
    assert status.status_code == 200
    assert terms.status_code == 200, terms.text
    assert terms.json()["two_factor_setup_required"] is True


async def test_once_enabled_the_owner_works_and_cannot_turn_it_off(
    client: AsyncClient, clock: FakeClock, required: None, restaurant_a: StaffedRestaurant
) -> None:
    secret, _ = await _enable(client, clock, OWNER_EMAIL)
    login = await client.post(
        url("auth/login"),
        json={"email": OWNER_EMAIL, "password": VALID_PASSWORD, "otp_code": _code(secret, clock)},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    staff = await client.get(STAFF_URL, headers=headers)
    me = await client.get(url("auth/me"), headers=headers)
    disabled = await client.delete(TWO_FACTOR_URL, headers=headers)

    assert staff.status_code == 200
    assert me.json()["two_factor_setup_required"] is False
    assert (disabled.status_code, disabled.json()["code"]) == (409, "two_factor_state_conflict")


async def test_a_waiter_does_not_need_it(
    client: AsyncClient, required: None, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.waiter)

    orders = await client.get(url("orders"), headers=headers)
    me = await client.get(url("auth/me"), headers=headers)

    assert orders.status_code == 200, orders.text
    assert me.json()["two_factor_setup_required"] is False


async def test_without_the_requirement_nothing_changes(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)

    staff = await client.get(STAFF_URL, headers=headers)
    me = await client.get(url("auth/me"), headers=headers)

    assert staff.status_code == 200
    assert me.json()["two_factor_setup_required"] is False
