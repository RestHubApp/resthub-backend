"""La hora del restaurante."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from resthub.core.local_time import (
    DEFAULT_TIMEZONE,
    is_valid_timezone,
    local_date,
    local_day_window,
)


def test_por_omision_es_la_hora_de_lima() -> None:
    assert DEFAULT_TIMEZONE == "America/Lima"


def test_la_noche_de_lima_ya_es_el_dia_siguiente_en_utc() -> None:
    # 21:00 en Lima son las 02:00 del día siguiente en UTC.
    moment = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)

    assert local_date(moment) == date(2026, 9, 25)


def test_la_ventana_del_dia_cubre_24_horas_locales() -> None:
    starts, ends = local_day_window(datetime(2026, 9, 26, 2, 0, tzinfo=UTC))

    assert starts.astimezone(UTC) == datetime(2026, 9, 25, 5, 0, tzinfo=UTC)
    assert ends - starts == timedelta(days=1)


def test_otra_zona_cambia_el_dia() -> None:
    moment = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)

    assert local_date(moment, "Europe/Madrid") == date(2026, 9, 26)


def test_solo_se_aceptan_zonas_iana() -> None:
    assert is_valid_timezone("America/Bogota")
    assert not is_valid_timezone("Lima")
    assert not is_valid_timezone("")
    assert not is_valid_timezone(" America/Lima")
    assert not is_valid_timezone("../etc/passwd")
