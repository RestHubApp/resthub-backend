"""Historial de inicios de sesión (RFS-15) y aviso de dispositivo o país nuevo (RFS-16)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.access_history import (
    AccessHistoryRow,
    AccessOutcome,
    LoginAttempt,
    SqlAccessHistory,
    describe_device,
    mask_address,
)
from resthub.core.geo import (
    UNKNOWN,
    Location,
    MaxMindGeoLocator,
    NoGeoLocator,
    get_geo_locator,
    open_geo_locator,
)
from resthub.core.permissions import Permission
from resthub.core.realtime import ACCESS_ALERT_TOPIC
from resthub.core.realtime_broker import LocalBroker
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from tests.conftest import (
    TEST_HASHER,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    staff_with_role,
    url,
)
from tests.fakes import FixedClock

LOGIN_URL = url("auth/login")
OWN_HISTORY_URL = url("auth/me/access-history")
ANDROID_CHROME = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
)
WINDOWS_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
IPHONE_SAFARI = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)


class CountryByAddress:
    """Ubicación de prueba: la ciudad y el país salen de una tabla fija."""

    def __init__(self, places: dict[str, Location]) -> None:
        self._places = places

    def locate(self, address: str) -> Location:
        return self._places.get(address, UNKNOWN)


async def _login(
    client: AsyncClient,
    email: str,
    password: str = VALID_PASSWORD,
    *,
    agent: str = ANDROID_CHROME,
    address: str = "200.48.1.7",
) -> int:
    response = await client.post(
        LOGIN_URL,
        json={"email": email, "password": password},
        headers={"user-agent": agent, "x-forwarded-for": address},
    )
    return response.status_code


async def _history(client: AsyncClient, headers: dict[str, str]) -> list[dict[str, object]]:
    response = await client.get(OWN_HISTORY_URL, headers=headers)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("200.48.1.7", "200.48.1.x"),
        ("2800:200:e840:1234::1", "2800:200:e840::/48"),
        ("desconocida", ""),
    ],
)
def test_address_is_stored_without_its_last_block(raw: str, masked: str) -> None:
    assert mask_address(raw) == masked


@pytest.mark.parametrize(
    ("agent", "device"),
    [
        (ANDROID_CHROME, "Chrome en Android"),
        (WINDOWS_FIREFOX, "Firefox en Windows"),
        (IPHONE_SAFARI, "Safari en iOS"),
        ("curl/8.5.0", "Desconocido"),
        ("Mozilla/5.0 (X11; Linux x86_64)", "Linux"),
    ],
)
def test_device_is_browser_and_system_without_versions(agent: str, device: str) -> None:
    assert describe_device(agent) == device


class FakeMmdb:
    def get(self, address: str) -> object:
        if address == "no-es-ip":
            raise ValueError(address)
        if address == "190.40.0.1":
            return {
                "city": {"names": {"es": "Lima", "en": "Lima"}},
                "country": {"iso_code": "PE"},
            }
        if address == "8.8.8.8":
            return {"country": {"iso_code": "US"}, "city": {"names": "raro"}}
        return None


def test_geo_locator_reads_city_and_country_from_the_local_database() -> None:
    locator = MaxMindGeoLocator(FakeMmdb())

    assert locator.locate("190.40.0.1") == Location(city="Lima", country="PE")
    assert locator.locate("8.8.8.8") == Location(city="", country="US")
    assert locator.locate("10.0.0.1") == UNKNOWN
    assert locator.locate("no-es-ip") == UNKNOWN


def test_without_a_database_access_is_recorded_without_location() -> None:
    assert isinstance(open_geo_locator(""), NoGeoLocator)
    assert isinstance(open_geo_locator("/no/existe/GeoLite2-City.mmdb"), NoGeoLocator)
    assert NoGeoLocator().locate("190.40.0.1") == UNKNOWN


async def test_each_attempt_is_listed_newest_first(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    email = restaurant_a.waiter.email
    assert await _login(client, email, "no-es-la-clave") == 401
    assert await _login(client, email, agent=WINDOWS_FIREFOX, address="190.40.0.1") == 200

    items = await _history(client, authorization_for(restaurant_a.waiter))

    assert [(item["outcome"], item["device"], item["network"]) for item in items] == [
        ("succeeded", "Firefox en Windows", "190.40.0.x"),
        ("failed", "Chrome en Android", "200.48.1.x"),
    ]


async def test_throttled_attempts_are_recorded_too(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    for _ in range(6):
        await _login(client, restaurant_a.waiter.email, "no-es-la-clave")

    items = await _history(client, authorization_for(restaurant_a.waiter))

    assert [item["outcome"] for item in items][:2] == ["throttled", "failed"]


async def test_attempt_with_an_unknown_email_belongs_to_nobody(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    await _login(client, "nadie@local-a.pe", "no-es-la-clave")

    row = (await session.execute(select(AccessHistoryRow))).scalar_one()

    assert (row.account_kind, row.account_id, row.restaurant_id) == ("", None, None)
    assert await _history(client, authorization_for(restaurant_a.admin)) == []


async def test_inactive_restaurant_attempt_is_recorded_as_blocked(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    restaurant_a.restaurant.is_active = False
    await SqlAlchemyRestaurantRepository(session).save(restaurant_a.restaurant)
    await session.commit()

    assert await _login(client, restaurant_a.waiter.email) == 403
    row = (await session.execute(select(AccessHistoryRow))).scalar_one()
    assert row.outcome == AccessOutcome.BLOCKED.value


async def test_manager_reads_the_history_of_their_staff_only(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    await _login(client, restaurant_a.waiter.email)
    cook = await staff_with_role(session, restaurant_a, "Cocinero", {Permission.MENU_READ.value})
    owner = authorization_for(restaurant_a.admin)

    own_staff = await client.get(
        url(f"staff/{restaurant_a.waiter.id}/access-history"), headers=owner
    )
    other_local = await client.get(
        url(f"staff/{restaurant_b.waiter.id}/access-history"), headers=owner
    )
    without_permission = await client.get(
        url(f"staff/{restaurant_a.waiter.id}/access-history"), headers=authorization_for(cook)
    )

    assert own_staff.status_code == 200
    assert [item["outcome"] for item in own_staff.json()["items"]] == ["succeeded"]
    assert other_local.status_code == 404
    assert without_permission.status_code == 403


async def test_platform_account_sees_its_own_history(
    client: AsyncClient, session: AsyncSession
) -> None:
    await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    login = await client.post(
        LOGIN_URL, json={"email": "equipo@resthub.dev", "password": VALID_PASSWORD}
    )

    items = await _history(client, {"Authorization": f"Bearer {login.json()['access_token']}"})

    assert [item["outcome"] for item in items] == ["succeeded"]


async def test_new_device_alerts_the_account_and_the_staff_managers(
    client: AsyncClient, restaurant_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    email = restaurant_a.waiter.email
    await _login(client, email)
    await _login(client, email)

    async with broker.subscribe() as queue:
        await _login(client, email, agent=IPHONE_SAFARI)
        await asyncio.sleep(0)
        alert = queue.get_nowait()

    items = await _history(client, authorization_for(restaurant_a.waiter))
    assert [item["new_device"] for item in items] == [True, False, False]
    assert alert.topic == ACCESS_ALERT_TOPIC
    assert alert.user_ids == {restaurant_a.waiter.id}
    assert alert.permissions == {Permission.STAFF_MANAGE.value}


async def test_new_country_is_flagged_from_the_local_location(
    app: FastAPI, client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    app.dependency_overrides[get_geo_locator] = lambda: CountryByAddress(
        {
            "190.40.0.1": Location(city="Lima", country="PE"),
            "81.2.69.160": Location(city="Londres", country="GB"),
        }
    )
    email = restaurant_a.waiter.email
    await _login(client, email, address="190.40.0.1")
    await _login(client, email, address="81.2.69.160")

    items = await _history(client, authorization_for(restaurant_a.waiter))

    assert [(item["city"], item["country"], item["new_country"]) for item in items] == [
        ("Londres", "GB", True),
        ("Lima", "PE", False),
    ]


async def test_history_is_purged_after_the_retention(session: AsyncSession) -> None:
    clock = FixedClock()
    history = SqlAccessHistory(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession),
        clock,
        NoGeoLocator(),
        retention_days=90,
    )
    attempt = LoginAttempt("nadie@local.pe", AccessOutcome.FAILED, "200.48.1.7", "")
    await history.record(attempt)
    clock.advance(days=89)
    await history.record(attempt)

    clock.advance(days=2)
    await history.record(attempt)
    remaining = await session.scalar(select(func.count()).select_from(AccessHistoryRow))

    assert remaining == 2
