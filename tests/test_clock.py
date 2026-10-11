"""El puerto `Clock`: el reloj del sistema y el fijo que usan las pruebas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from resthub.core.clock import SystemClock, utc_now
from tests.fakes import FIXED_NOW, FixedClock


def test_system_clock_returns_the_current_instant_in_utc() -> None:
    first = SystemClock().now()
    second = SystemClock().now()

    assert first.tzinfo is UTC
    assert first <= second


def test_utc_now_is_the_system_clock_with_timezone() -> None:
    assert utc_now().utcoffset() == timedelta(0)


def test_fixed_clock_does_not_move_until_the_test_moves_it() -> None:
    clock = FixedClock()

    assert clock.now() == clock.now() == FIXED_NOW

    clock.advance(hours=2, minutes=30)
    assert clock.now() == FIXED_NOW + timedelta(hours=2, minutes=30)

    clock.set(datetime(2030, 1, 1, tzinfo=UTC))
    assert clock.now() == datetime(2030, 1, 1, tzinfo=UTC)


def test_fixed_clock_rejects_a_time_without_timezone() -> None:
    with pytest.raises(ValueError, match="zona"):
        FixedClock(datetime(2026, 1, 1))  # noqa: DTZ001 - el caso es justo la hora sin zona
