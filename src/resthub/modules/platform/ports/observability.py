"""Lectura de la telemetría que guarda el núcleo, para el panel de observabilidad.

Las cuentas exactas (peticiones, errores, promedios) las hace la base. Las
duraciones se traen crudas para calcular percentiles en Python; con `every`
mayor que 1 se trae una de cada `every` filas por identificador, que es un
muestreo uniforme sin ordenar al azar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from resthub.modules.platform.domain.observability import (
    LogDetail,
    LogEntry,
    LogLevel,
    RequestEntry,
    StatusCount,
    TelemetryWindow,
)


@dataclass(frozen=True, slots=True)
class RequestTotals:
    requests: int
    errors_5xx: int
    errors_4xx: int
    avg_db_ms: float
    active_restaurants: int


@dataclass(frozen=True, slots=True)
class BucketTotals:
    # Inicio del cubo en segundos desde 1970, múltiplo del ancho del cubo.
    start_epoch: int
    requests: int
    errors_5xx: int


@dataclass(frozen=True, slots=True)
class RouteTotals:
    method: str
    route: str
    requests: int
    errors_5xx: int
    avg_db_ms: float


@dataclass(frozen=True, slots=True)
class LogQuery:
    window: TelemetryWindow
    limit: int
    level: LogLevel | None = None
    # Texto a buscar en el evento y en el JSON de sus campos, sin mayúsculas.
    search: str | None = None
    request_id: str | None = None
    before_id: int | None = None


@dataclass(frozen=True, slots=True)
class RequestQuery:
    window: TelemetryWindow
    limit: int
    status_min: int | None = None
    route: str | None = None
    request_id: str | None = None
    before_id: int | None = None


class TelemetryReader(Protocol):
    async def request_totals(self, window: TelemetryWindow) -> RequestTotals: ...

    async def durations(
        self, window: TelemetryWindow, *, every: int = 1, route: tuple[str, str] | None = None
    ) -> list[float]:
        """Duraciones en milisegundos; `route` es `(método, plantilla)`."""
        ...

    async def timed_durations(
        self, window: TelemetryWindow, *, every: int = 1
    ) -> list[tuple[datetime, float]]: ...

    async def route_durations(
        self, window: TelemetryWindow, *, every: int = 1
    ) -> list[tuple[str, str, float]]:
        """`(método, plantilla, duración)` de cada petición."""
        ...

    async def bucket_totals(
        self, window: TelemetryWindow, bucket_seconds: int
    ) -> list[BucketTotals]: ...

    async def route_totals(self, window: TelemetryWindow) -> list[RouteTotals]: ...

    async def status_counts(self, window: TelemetryWindow) -> list[StatusCount]:
        """Ordenado por estado."""
        ...

    async def logs(self, query: LogQuery) -> list[LogEntry]:
        """Lo más nuevo primero (por identificador), hasta `query.limit` filas."""
        ...

    async def log(self, entry_id: int) -> LogDetail | None: ...

    async def requests(self, query: RequestQuery) -> list[RequestEntry]:
        """Lo más nuevo primero (por identificador), hasta `query.limit` filas."""
        ...
