"""El horario de atención del local y si un instante cae dentro (RFS-32).

Cada tramo es un día de la semana (0 = lunes) con su hora de apertura y de
cierre en la hora del local. Un cierre igual o anterior a la apertura cruza la
medianoche: «18:00 a 02:00» del viernes sigue abierto el sábado a la 01:00. Un
día sin tramos es un día cerrado. Sin horario (lista vacía) no se marca nada.

Python puro: lo usan el dominio de restaurantes y el historial de accesos.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

DAYS_IN_WEEK = 7
_HHMM = re.compile(r"[0-9]{2}:[0-9]{2}")
# Con dos turnos por día (almuerzo y cena) sobra; el tope evita listas absurdas.
MAX_PERIODS = DAYS_IN_WEEK * 3


class InvalidOpeningHoursError(ValueError):
    """El horario no tiene sentido: un día inexistente, una hora mal escrita o demasiados tramos."""


@dataclass(frozen=True, slots=True)
class OpeningPeriod:
    day: int
    opens: time
    closes: time

    @property
    def crosses_midnight(self) -> bool:
        return self.closes <= self.opens

    def as_dict(self) -> dict[str, Any]:
        return {"day": self.day, "opens": _hhmm(self.opens), "closes": _hhmm(self.closes)}


def _hhmm(moment: time) -> str:
    return moment.strftime("%H:%M")


def _parse_time(raw: object) -> time:
    text = str(raw).strip()
    message = f"La hora {text!r} no es válida: escríbela como 08:30."
    if not _HHMM.fullmatch(text):
        raise InvalidOpeningHoursError(message)
    try:
        return time.fromisoformat(text)
    except ValueError as error:
        raise InvalidOpeningHoursError(message) from error


def parse_period(raw: Mapping[str, Any]) -> OpeningPeriod:
    day = raw.get("day")
    if not isinstance(day, int) or isinstance(day, bool) or not 0 <= day < DAYS_IN_WEEK:
        message = "El día del horario va de 0 (lunes) a 6 (domingo)."
        raise InvalidOpeningHoursError(message)
    return OpeningPeriod(day, _parse_time(raw.get("opens")), _parse_time(raw.get("closes")))


def validate_opening_hours(periods: Iterable[OpeningPeriod]) -> tuple[OpeningPeriod, ...]:
    ordered = tuple(sorted(periods, key=lambda period: (period.day, period.opens)))
    if any(not 0 <= period.day < DAYS_IN_WEEK for period in ordered):
        message = "El día del horario va de 0 (lunes) a 6 (domingo)."
        raise InvalidOpeningHoursError(message)
    if any(period.opens == period.closes for period in ordered):
        message = "Un tramo del horario no puede abrir y cerrar a la misma hora."
        raise InvalidOpeningHoursError(message)
    if len(ordered) > MAX_PERIODS:
        message = f"El horario admite hasta {MAX_PERIODS} tramos."
        raise InvalidOpeningHoursError(message)
    return ordered


def parse_opening_hours(raw: object) -> tuple[OpeningPeriod, ...]:
    """El horario guardado (lista de `{day, opens, closes}`); vacío si no hay."""
    if not isinstance(raw, list):
        return ()
    return validate_opening_hours(parse_period(item) for item in raw if isinstance(item, Mapping))


def _covers(period: OpeningPeriod, weekday: int, clock: time) -> bool:
    if not period.crosses_midnight:
        return period.day == weekday and period.opens <= clock < period.closes
    if period.day == weekday:
        return clock >= period.opens
    return (period.day + 1) % DAYS_IN_WEEK == weekday and clock < period.closes


def is_within_hours(periods: Iterable[OpeningPeriod], moment: datetime, timezone: str) -> bool:
    """Si el local está abierto en `moment` (un instante con zona) según su horario."""
    local = moment.astimezone(ZoneInfo(timezone))
    clock = local.time().replace(tzinfo=None)
    return any(_covers(period, local.weekday(), clock) for period in periods)


def is_outside_hours(periods: Iterable[OpeningPeriod], moment: datetime, timezone: str) -> bool:
    """Si el instante cae fuera del horario; sin horario, nunca."""
    listed = tuple(periods)
    return bool(listed) and not is_within_hours(listed, moment, timezone)
