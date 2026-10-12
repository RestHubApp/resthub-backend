"""El rango de días que mira un reporte.

Son días del restaurante, no de UTC: "las ventas del lunes" son las de los
pedidos que el local abrió el lunes según su propio reloj (ver `local_time`).
Los dos extremos entran.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from resthub.core.local_time import local_date, local_midnight
from resthub.modules.insights.domain.exceptions import InvalidPeriodError

# Lo que mira el panel si no se pide otra cosa: el último mes.
DEFAULT_DAYS = 30
# Un año y un día: alcanza para comparar un mes con el mismo del año anterior
# y le pone techo a lo que una sola consulta puede recorrer.
MAX_DAYS = 366
# Hasta cuánto atrás y adelante de «hoy» (del restaurante) acepta fechas un reporte. Son
# límites de negocio y se mueven con el tiempo: un valor fijo caducaría.
MAX_YEARS_BACK = 50
MAX_YEARS_AHEAD = 5
# Guarda técnica del constructor de `DateRange`: `previous()` resta hasta 2 x MAX_DAYS y
# `window()` suma un día, y sin margen una fecha como 0001-01-01 o 9999-12-31 desbordaba
# `date` (`OverflowError`) y la API respondía 500. Se mide desde `date.min`/`date.max`,
# así que nunca caduca.
_OVERFLOW_MARGIN = timedelta(days=3 * MAX_DAYS)
_TECHNICAL_MIN = date.min + _OVERFLOW_MARGIN
_TECHNICAL_MAX = date.max - _OVERFLOW_MARGIN


def _shift_years(day: date, years: int) -> date:
    """`day` movido `years` años; el 29 de febrero cae el 28 si el año no es bisiesto."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def supported_bounds(today: date) -> tuple[date, date]:
    """El primer y el último día que un reporte acepta, según el «hoy» del restaurante."""
    return _shift_years(today, -MAX_YEARS_BACK), _shift_years(today, MAX_YEARS_AHEAD)


def check_supported(day: date, today: date) -> None:
    earliest, latest = supported_bounds(today)
    if not earliest <= day <= latest:
        message = f"Las fechas deben estar entre {earliest.isoformat()} y {latest.isoformat()}."
        raise InvalidPeriodError(message)


def _check_representable(day: date) -> None:
    if not _TECHNICAL_MIN <= day <= _TECHNICAL_MAX:
        message = "La fecha está fuera del rango que el sistema puede representar."
        raise InvalidPeriodError(message)


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        _check_representable(self.start)
        _check_representable(self.end)
        if self.start > self.end:
            message = "La fecha inicial es posterior a la final."
            raise InvalidPeriodError(message)
        if self.days > MAX_DAYS:
            message = f"El rango admite como máximo {MAX_DAYS} días."
            raise InvalidPeriodError(message)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def each_day(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range(self.days)]

    def previous(self) -> DateRange:
        """El rango del mismo largo que termina justo antes de este."""
        end = self.start - timedelta(days=1)
        return DateRange(start=end - timedelta(days=self.days - 1), end=end)

    def window(self, timezone: str) -> tuple[datetime, datetime]:
        """Los instantes en que empieza y termina el rango, para filtrar por hora.

        El final queda afuera: es la medianoche que abre el día siguiente.
        """
        return (
            local_midnight(self.start, timezone),
            local_midnight(self.end + timedelta(days=1), timezone),
        )


def resolve_range(
    date_from: date | None, date_to: date | None, now: datetime, timezone: str
) -> DateRange:
    """Completa lo que el cliente no mandó.

    Sin nada, los últimos treinta días hasta hoy. Con solo el final, los
    treinta días que terminan ahí; con solo el inicio, desde ahí hasta hoy.
    """
    today = local_date(now, timezone)
    for given in (date_from, date_to):
        if given is not None:
            check_supported(given, today)
    if date_to is None:
        date_to = max(today, date_from) if date_from is not None else today
    if date_from is None:
        date_from = date_to - timedelta(days=DEFAULT_DAYS - 1)
    return DateRange(start=date_from, end=date_to)
