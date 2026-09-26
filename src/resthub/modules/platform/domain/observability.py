"""El panel de observabilidad: ventanas, cubos, percentiles y lo que se muestra.

La captura es del núcleo (`core/telemetry.py`); acá solo se lee lo guardado.
Los percentiles se calculan en Python, no en la base: SQLite, que es la base
de las pruebas, no tiene `percentile_cont`, y calcularlos igual en los dos
lados es la forma de que las pruebas digan algo de producción.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

# Por encima de estas filas en la ventana, las duraciones se muestrean de forma
# uniforme (una de cada `k` por identificador) y la respuesta lo dice.
SAMPLE_LIMIT = 200_000
MAX_SEARCH_LENGTH = 120
DEFAULT_ROUTES_LIMIT = 20
MAX_ROUTES_LIMIT = 100
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200


class Window(StrEnum):
    HOUR = "1h"
    SIX_HOURS = "6h"
    DAY = "24h"
    WEEK = "7d"

    @property
    def span(self) -> timedelta:
        return _SPANS[self]

    @property
    def bucket_seconds(self) -> int:
        """El ancho de cada punto de la serie temporal."""
        return _BUCKETS[self]


_SPANS = {
    Window.HOUR: timedelta(hours=1),
    Window.SIX_HOURS: timedelta(hours=6),
    Window.DAY: timedelta(hours=24),
    Window.WEEK: timedelta(days=7),
}
_BUCKETS = {
    Window.HOUR: 60,
    Window.SIX_HOURS: 5 * 60,
    Window.DAY: 15 * 60,
    Window.WEEK: 2 * 60 * 60,
}


class LogLevel(StrEnum):
    WARNING = "warning"
    # Incluye lo que se logueó como `critical`: la captura lo guarda como error.
    ERROR = "error"


class RouteSort(StrEnum):
    REQUESTS = "requests"
    P95 = "p95"
    ERRORS = "errors"


@dataclass(frozen=True, slots=True)
class TelemetryWindow:
    """El tramo que se mira: `[since, until]`, de un restaurante o de todos."""

    since: datetime
    until: datetime
    restaurant_id: int | None = None

    @classmethod
    def ending_at(
        cls, window: Window, now: datetime, restaurant_id: int | None = None
    ) -> TelemetryWindow:
        return cls(since=now - window.span, until=now, restaurant_id=restaurant_id)


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Percentil con interpolación lineal entre los dos vecinos, como `numpy`.

    `sorted_values` tiene que venir ordenado. Sin valores es cero: el panel
    muestra ceros en una ventana vacía, igual que en los cubos sin datos.
    """
    if not sorted_values:
        return 0.0
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    value = sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight
    return round(value, 1)


def sampling_step(rows: int, limit: int = SAMPLE_LIMIT) -> int:
    """Cada cuántas filas se toma una para no pasar de `limit`; 1 es todas."""
    return 1 if rows <= limit else math.ceil(rows / limit)


def ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


@dataclass(frozen=True, slots=True)
class Summary:
    window: Window
    requests: int
    errors_5xx: int
    errors_4xx: int
    # Fracción de peticiones con 5xx, de 0 a 1. Los 4xx son del cliente (un
    # token vencido, un formulario inválido) y no cuentan como falla.
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    avg_db_ms: float
    active_restaurants: int
    dropped_events: int
    sampled: bool


@dataclass(frozen=True, slots=True)
class TimePoint:
    t: datetime
    requests: int
    errors_5xx: int
    p95_ms: float


@dataclass(frozen=True, slots=True)
class Timeseries:
    bucket_seconds: int
    points: list[TimePoint]
    sampled: bool


@dataclass(frozen=True, slots=True)
class RouteStats:
    method: str
    route: str
    requests: int
    errors_5xx: int
    p50_ms: float
    p95_ms: float
    avg_db_ms: float
    sampled: bool


@dataclass(frozen=True, slots=True)
class StatusCount:
    status: int
    count: int


@dataclass(frozen=True, slots=True)
class LogEntry:
    id: int
    at: datetime
    level: str
    logger: str
    event: str
    request_id: str | None
    restaurant_id: int | None
    has_traceback: bool


@dataclass(frozen=True, slots=True)
class LogDetail:
    entry: LogEntry
    fields: dict[str, Any]
    traceback: str | None


@dataclass(frozen=True, slots=True)
class RequestEntry:
    id: int
    at: datetime
    method: str
    route: str
    status: int
    duration_ms: float
    db_ms: float
    db_queries: int
    request_id: str
    account_kind: str
    restaurant_id: int | None
    account_id: int | None


@dataclass(frozen=True, slots=True)
class KeysetPage[T]:
    """Lo más nuevo primero. `next_before_id` pide la página siguiente; `None` es la última."""

    items: list[T] = field(default_factory=list)
    next_before_id: int | None = None
