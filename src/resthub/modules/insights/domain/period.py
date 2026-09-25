"""El rango de días que mira un reporte.

Son días del restaurante, no de UTC: "las ventas del lunes" son las de los
pedidos que el local abrió el lunes según su propio reloj (ver `local_time`).
Los dos extremos entran.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from resthub.core.local_time import local_date, local_midnight
from resthub.modules.insights.domain.exceptions import InvalidPeriod

# Lo que mira el panel si no se pide otra cosa: el último mes.
DEFAULT_DAYS = 30
# Un año y un día: alcanza para comparar un mes con el mismo del año anterior
# y le pone techo a lo que una sola consulta puede recorrer.
MAX_DAYS = 366


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise InvalidPeriod("La fecha inicial es posterior a la final.")
        if self.days > MAX_DAYS:
            raise InvalidPeriod(f"El rango admite como máximo {MAX_DAYS} días.")

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
    if date_to is None:
        date_to = max(today, date_from) if date_from is not None else today
    if date_from is None:
        date_from = date_to - timedelta(days=DEFAULT_DAYS - 1)
    return DateRange(start=date_from, end=date_to)
