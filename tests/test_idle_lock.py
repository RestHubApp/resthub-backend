"""Bloqueo de la sesión por inactividad y desbloqueo con PIN (RFS-29)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from resthub.core.activity import ActivityKind
from tests.conftest import (
    VALID_PASSWORD,
    FakeClock,
    StaffedRestaurant,
    authorization_for,
    logged_events,
    url,
)

ME_URL = url("auth/me")
PIN_URL = url("auth/me/pin")
LOCK_URL = url("auth/me/lock")
UNLOCK_URL = url("auth/me/unlock")
RESTAURANT_URL = url("restaurant")
WAITER_EMAIL = "mesero@local-a.pe"


async def _login(client: AsyncClient, email: str = WAITER_EMAIL) -> dict[str, str]:
    response = await client.post(
        url("auth/login"), json={"email": email, "password": VALID_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _with_pin(client: AsyncClient, pin: str = "4321") -> dict[str, str]:
    headers = await _login(client)
    await client.post(
        url("auth/me/reauthenticate"), json={"password": VALID_PASSWORD}, headers=headers
    )
    assert (await client.put(PIN_URL, json={"pin": pin}, headers=headers)).status_code == 204
    return headers


async def test_choosing_a_pin_asks_for_the_password_first(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client)

    blocked = await client.put(PIN_URL, json={"pin": "4321"}, headers=headers)
    await client.post(
        url("auth/me/reauthenticate"), json={"password": VALID_PASSWORD}, headers=headers
    )
    chosen = await client.put(PIN_URL, json={"pin": "4321"}, headers=headers)
    session = (await client.get(ME_URL, headers=headers)).json()

    assert blocked.json()["code"] == "reauthentication_required"
    assert chosen.status_code == 204
    assert (session["has_pin"], session["idle_lock_minutes"], session["session_locked"]) == (
        True,
        10,
        False,
    )


async def test_a_pin_has_four_to_six_digits(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.waiter)

    letters = await client.put(PIN_URL, json={"pin": "12ab"}, headers=headers)
    short = await client.put(PIN_URL, json={"pin": "123"}, headers=headers)
    six = await client.put(PIN_URL, json={"pin": "123456"}, headers=headers)

    assert (letters.status_code, letters.json()["code"]) == (422, "invalid_pin")
    assert short.status_code == 422
    assert six.status_code == 204


async def test_a_locked_session_only_unlocks(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _with_pin(client)

    assert (await client.post(LOCK_URL, headers=headers)).status_code == 204
    blocked = await client.get(RESTAURANT_URL, headers=headers)
    session = (await client.get(ME_URL, headers=headers)).json()
    refreshed = await client.post(url("auth/refresh"), headers=headers)
    unlocked = await client.post(UNLOCK_URL, json={"pin": "4321"}, headers=headers)
    after = await client.get(RESTAURANT_URL, headers=headers)

    assert (blocked.status_code, blocked.json()["code"]) == (423, "session_locked")
    assert session["session_locked"] is True
    assert refreshed.status_code == 200
    assert refreshed.json()["session_locked"] is True
    assert unlocked.status_code == 204
    assert after.status_code == 200


async def test_the_third_wrong_pin_closes_the_session(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _with_pin(client)
    await client.post(LOCK_URL, headers=headers)

    first = await client.post(UNLOCK_URL, json={"pin": "0000"}, headers=headers)
    second = await client.post(UNLOCK_URL, json={"pin": "1111"}, headers=headers)
    third = await client.post(UNLOCK_URL, json={"pin": "2222"}, headers=headers)

    assert (first.status_code, first.json()["code"]) == (400, "wrong_pin")
    assert first.json()["detail"] == "El PIN no es correcto. Te quedan 2 intentos."
    assert second.json()["detail"] == "El PIN no es correcto. Te quedan 1 intento."
    assert (third.status_code, third.json()["code"]) == (401, "unlock_attempts_exhausted")
    assert (await client.get(ME_URL, headers=headers)).status_code == 401
    activity = await client.get(url("activity"), headers=authorization_for(restaurant_a.admin))
    kinds = {item["kind"] for item in activity.json()["items"]}
    assert ActivityKind.SESSION_LOCKED_OUT.value in kinds


async def test_a_session_unused_for_the_restaurant_minutes_locks_itself(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client)

    clock.advance(9 * 60)
    still_open = await client.get(RESTAURANT_URL, headers=headers)
    clock.advance(11 * 60)
    locked = await client.get(RESTAURANT_URL, headers=headers)

    assert still_open.status_code == 200
    assert locked.status_code == 423


async def test_the_restaurant_can_turn_it_off(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    off = await client.patch(RESTAURANT_URL, json={"idle_lock_minutes": 0}, headers=owner)
    too_long = await client.patch(RESTAURANT_URL, json={"idle_lock_minutes": 500}, headers=owner)
    headers = await _login(client)

    clock.advance(30 * 60)

    assert off.json()["idle_lock_minutes"] == 0
    assert too_long.status_code == 422
    assert (await client.get(RESTAURANT_URL, headers=headers)).status_code == 200
    assert (await client.get(ME_URL, headers=headers)).json()["idle_lock_minutes"] == 0


async def test_without_a_pin_the_password_unlocks(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client)
    await client.post(LOCK_URL, headers=headers)

    with_pin = await client.post(UNLOCK_URL, json={"pin": "4321"}, headers=headers)
    both = await client.post(
        UNLOCK_URL, json={"pin": "4321", "password": VALID_PASSWORD}, headers=headers
    )
    with_password = await client.post(
        UNLOCK_URL, json={"password": VALID_PASSWORD}, headers=headers
    )

    assert (with_pin.status_code, with_pin.json()["code"]) == (409, "pin_not_set")
    assert both.status_code == 422
    assert with_password.status_code == 204
    assert (await client.get(RESTAURANT_URL, headers=headers)).status_code == 200


async def test_removing_the_pin(client: AsyncClient, restaurant_a: StaffedRestaurant) -> None:
    headers = await _with_pin(client)

    assert (await client.delete(PIN_URL, headers=headers)).status_code == 204
    assert (await client.get(ME_URL, headers=headers)).json()["has_pin"] is False


async def test_unlocking_an_open_session_does_nothing(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.waiter)

    response = await client.post(UNLOCK_URL, json={"pin": "9999"}, headers=headers)

    assert response.status_code == 204


async def test_every_wrong_unlock_is_logged(
    client: AsyncClient, restaurant_a: StaffedRestaurant, caplog: pytest.LogCaptureFixture
) -> None:
    headers = await _with_pin(client, pin="4321")
    await client.post(LOCK_URL, headers=headers)

    await client.post(UNLOCK_URL, json={"pin": "0000"}, headers=headers)
    await client.post(UNLOCK_URL, json={"password": "otra-clave-cualquiera"}, headers=headers)

    events = logged_events(caplog, "auth.unlock_failed")
    assert [(e["method"], e["failures"], e["attempts_left"]) for e in events] == [
        ("pin", 1, 2),
        ("password", 2, 1),
    ]
    assert all(e["user_id"] == restaurant_a.waiter.id for e in events)
    assert "0000" not in caplog.text
    assert "otra-clave-cualquiera" not in caplog.text
