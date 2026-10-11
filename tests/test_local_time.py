"""La hora del restaurante."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from resthub.core.local_time import (
    DEFAULT_TIMEZONE,
    is_valid_timezone,
    local_date,
    local_day_window,
)


def test_default_is_lima_time() -> None:
    assert DEFAULT_TIMEZONE == "America/Lima"


def test_lima_night_is_already_next_day_in_utc() -> None:
    # 21:00 en Lima son las 02:00 del día siguiente en UTC.
    moment = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)

    assert local_date(moment) == date(2026, 9, 25)


def test_day_window_covers_24_local_hours() -> None:
    starts, ends = local_day_window(datetime(2026, 9, 26, 2, 0, tzinfo=UTC))

    assert starts.astimezone(UTC) == datetime(2026, 9, 25, 5, 0, tzinfo=UTC)
    assert ends - starts == timedelta(days=1)


def test_other_timezone_changes_the_day() -> None:
    moment = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)

    assert local_date(moment, "Europe/Madrid") == date(2026, 9, 26)


def test_only_iana_timezones_are_accepted() -> None:
    assert is_valid_timezone("America/Bogota")
    assert not is_valid_timezone("Lima")
    assert not is_valid_timezone("")
    assert not is_valid_timezone(" America/Lima")
    assert not is_valid_timezone("../etc/passwd")
