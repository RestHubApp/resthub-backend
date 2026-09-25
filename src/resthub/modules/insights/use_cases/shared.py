"""Piezas comunes a los casos de uso de indicadores."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from resthub.core.identity import Role
from resthub.core.realtime import EventPublisher, RealtimeEvent
from resthub.modules.insights.domain.period import DateRange, resolve_range
from resthub.modules.insights.ports.restaurant_calendar import RestaurantCalendar

# El panel y el tablero del encargado escuchan este tema: cuando llega una
# clasificación nueva vuelven a pedir los datos con sus permisos.
INSIGHTS_TOPIC = "insights"
# Cuántas preguntas se le hacen a la vez al motor de decisiones. Jev acepta
# muchas más por minuto; el límite es para no abrir treinta conexiones de golpe
# desde un servidor chico.
MAX_PARALLEL_DECISIONS = 6


def announce(events: EventPublisher, restaurant_id: int, reference_id: int | None = None) -> None:
    # Solo al encargado: es el único rol con `insights.read`.
    events.publish(
        RealtimeEvent(
            restaurant_id=restaurant_id,
            topic=INSIGHTS_TOPIC,
            roles=frozenset({Role.ADMIN}),
            reference_id=reference_id,
        )
    )


async def decide_all[T, R](
    items: Iterable[T], decide: Callable[[T], Awaitable[R]], limit: int = MAX_PARALLEL_DECISIONS
) -> list[R]:
    """Pregunta por cada elemento en paralelo, de a `limit`, y conserva el orden."""
    gate = asyncio.Semaphore(limit)

    async def one(item: T) -> R:
        async with gate:
            return await decide(item)

    return list(await asyncio.gather(*(one(item) for item in items)))


@dataclass(frozen=True, slots=True)
class PeriodQuery:
    restaurant_id: int
    date_from: date | None = None
    date_to: date | None = None


@dataclass(frozen=True, slots=True)
class Report[T]:
    """Un reporte con el rango que de verdad se miró y la zona en que se contó."""

    period: DateRange
    timezone: str
    data: T


async def resolve_period(calendar: RestaurantCalendar, query: PeriodQuery) -> tuple[DateRange, str]:
    timezone = await calendar.timezone(query.restaurant_id)
    return resolve_range(query.date_from, query.date_to, datetime.now(UTC), timezone), timezone
