"""Horario de atención del local y accesos fuera de él (RFS-32)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.access_history import AccessOutcome, LoginAttempt, SqlAccessHistory
from resthub.core.geo import NoGeoLocator
from resthub.core.opening_hours import (
    InvalidOpeningHoursError,
    OpeningPeriod,
    is_outside_hours,
    parse_opening_hours,
)
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from tests.conftest import StaffedRestaurant, authorization_for, url
from tests.fakes import FixedClock

LIMA = "America/Lima"
# Lunes 15 de junio de 2026: a las 12:00 UTC son las 07:00 en Lima.
MONDAY_7_AM = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
LUNCH_AND_DINNER = (
    OpeningPeriod(0, time.fromisoformat("12:00"), time.fromisoformat("16:00")),
    OpeningPeriod(0, time.fromisoformat("19:00"), time.fromisoformat("23:00")),
)


def _lima(day: int, hour: int, minute: int = 0) -> datetime:
    """Un instante de la semana del 15 de junio (lunes = 0) a esa hora de Lima."""
    return datetime(2026, 6, 15 + day, hour, minute, tzinfo=ZoneInfo(LIMA))


@pytest.mark.parametrize(
    ("moment", "outside"),
    [
        (_lima(0, 7), True),
        (_lima(0, 12), False),
        (_lima(0, 15, 59), False),
        (_lima(0, 16), True),
        (_lima(0, 22, 30), False),
        # El martes no tiene tramos: está cerrado.
        (_lima(1, 13), True),
    ],
)
def test_split_shifts_and_closed_days(moment: datetime, outside: bool) -> None:
    assert is_outside_hours(LUNCH_AND_DINNER, moment, LIMA) is outside


def test_a_closing_before_opening_crosses_midnight() -> None:
    friday_night = (OpeningPeriod(4, time.fromisoformat("18:00"), time.fromisoformat("02:00")),)

    assert is_outside_hours(friday_night, _lima(4, 23), LIMA) is False
    assert is_outside_hours(friday_night, _lima(5, 1, 30), LIMA) is False
    assert is_outside_hours(friday_night, _lima(5, 2), LIMA) is True


def test_without_hours_nothing_is_outside() -> None:
    assert is_outside_hours((), MONDAY_7_AM, LIMA) is False


@pytest.mark.parametrize(
    "raw",
    [
        {"day": 7, "opens": "08:00", "closes": "12:00"},
        {"day": 0, "opens": "25:00", "closes": "12:00"},
        {"day": 0, "opens": "08:00", "closes": "08:00"},
        {"day": True, "opens": "08:00", "closes": "12:00"},
    ],
)
def test_invalid_periods_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(InvalidOpeningHoursError):
        parse_opening_hours([raw])


async def test_the_owner_sets_and_clears_the_hours(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    hours = [
        {"day": 0, "opens": "19:00", "closes": "23:00"},
        {"day": 0, "opens": "12:00", "closes": "16:00"},
    ]

    saved = await client.patch(url("restaurant"), json={"opening_hours": hours}, headers=owner)
    wrong = await client.patch(
        url("restaurant"),
        json={"opening_hours": [{"day": 0, "opens": "08:00", "closes": "08:00"}]},
        headers=owner,
    )
    cleared = await client.patch(url("restaurant"), json={"opening_hours": []}, headers=owner)

    assert saved.status_code == 200, saved.text
    assert saved.json()["opening_hours"] == [
        {"day": 0, "opens": "12:00:00", "closes": "16:00:00"},
        {"day": 0, "opens": "19:00:00", "closes": "23:00:00"},
    ]
    assert (wrong.status_code, wrong.json()["code"]) == (422, "invalid_opening_hours")
    assert cleared.json()["opening_hours"] == []


async def test_an_access_outside_the_hours_is_flagged(
    session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    restaurants = SqlAlchemyRestaurantRepository(session)
    restaurant = restaurant_a.restaurant
    restaurant.set_opening_hours(LUNCH_AND_DINNER)
    await restaurants.save(restaurant)
    await session.commit()
    clock = FixedClock(MONDAY_7_AM)
    history = SqlAccessHistory(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession),
        clock,
        NoGeoLocator(),
    )
    attempt = LoginAttempt(restaurant_a.waiter.email, AccessOutcome.FAILED, "200.48.1.7", "")

    early = await history.record(attempt)
    clock.set(_lima(0, 13))
    at_lunch = await history.record(attempt)

    assert early.outside_hours is True
    assert at_lunch.outside_hours is False
