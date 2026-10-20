"""Vida máxima de una sesión de dispositivo (ASVS V7.3.2, V7.1.1)."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.access_history import STAFF_ACCOUNT
from resthub.core.account_sessions import SessionOrigin, SqlSessionRegistry
from resthub.core.config import Settings
from resthub.core.geo import NoGeoLocator
from tests.conftest import VALID_PASSWORD, FakeClock, StaffedRestaurant, url
from tests.fakes import FixedClock

LOGIN_URL = url("auth/login")
REFRESH_URL = url("auth/refresh")
ME_URL = url("auth/me")
# Menos que la vida de un token: la sesión nunca queda sin usar lo bastante
# para cerrarse por inactividad; solo la alcanza la vida máxima.
REFRESH_EVERY_SECONDS = 50 * 60


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(LOGIN_URL, json={"email": email, "password": VALID_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_settings_session_absolute_lifetime_defaults_to_twelve_hours() -> None:
    assert Settings().session_absolute_lifetime_hours == 12


async def test_refresh_does_not_extend_the_session_past_its_absolute_lifetime(
    client: AsyncClient, restaurant_a: StaffedRestaurant, clock: FakeClock
) -> None:
    headers = await _login(client, restaurant_a.waiter.email)
    elapsed = 0
    # Renovada cada 50 minutos, la sesión sigue viva hasta las 12 horas.
    while elapsed + REFRESH_EVERY_SECONDS < 12 * 3600:
        clock.advance(REFRESH_EVERY_SECONDS)
        elapsed += REFRESH_EVERY_SECONDS
        response = await client.post(REFRESH_URL, headers=headers)
        assert response.status_code == 200, (elapsed, response.text)
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    clock.advance(REFRESH_EVERY_SECONDS)
    refreshed = await client.post(REFRESH_URL, headers=headers)
    read = await client.get(ME_URL, headers=headers)

    assert refreshed.status_code == 401
    assert refreshed.json()["code"] == "unauthenticated"
    assert refreshed.json()["detail"] == "La sesión venció: vuelve a iniciar sesión."
    assert read.status_code == 401
    assert read.json()["code"] == "unauthenticated"


async def test_session_registry_drops_expired_sessions_from_the_open_list(
    session: AsyncSession,
) -> None:
    clock = FixedClock()
    registry = SqlSessionRegistry(
        clock=clock,
        geo=NoGeoLocator(),
        token_ttl=timedelta(hours=1),
        absolute_lifetime=timedelta(hours=2),
    )
    state = await registry.open(
        session,
        session_id="sesion-larga",
        account_kind=STAFF_ACCOUNT,
        account_id=7,
        restaurant_id=1,
        origin=SessionOrigin("200.48.1.10", ""),
    )
    clock.advance(minutes=110)
    await registry.touch(session, state)
    fresh = await registry.find(session, state.id)
    assert fresh is not None
    assert registry.is_alive(fresh)
    assert [each.id for each in await registry.list_open(session, STAFF_ACCOUNT, 7)] == [state.id]

    clock.advance(minutes=11)

    assert registry.has_expired(fresh)
    assert not registry.is_alive(fresh)
    assert await registry.list_open(session, STAFF_ACCOUNT, 7) == []
