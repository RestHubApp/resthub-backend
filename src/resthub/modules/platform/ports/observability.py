"""Lectura de la telemetría que guarda el núcleo, para el panel de observabilidad.

Las cuentas exactas (peticiones, errores, promedios) las hace la base. Los
percentiles de la duración se piden ya calculados, con interpolación lineal
(`domain.observability.percentile`): el adaptador decide si los calcula la
base o Python. Con `every` mayor que 1 se usa una de cada `every` filas por
identificador, que es un muestreo uniforme sin ordenar al azar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
    # No usa índice: recorre las filas de la ventana (que sí lo usa), y los
    # campos de cada una tienen tope (`core/telemetry.py`).
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

    async def duration_percentiles(
        self, window: TelemetryWindow, fractions: Sequence[float], *, every: int = 1
    ) -> list[float]:
        """Un percentil (en ms) por cada fracción, en su orden; ceros sin filas."""
        ...

    async def bucket_percentiles(
        self,
        window: TelemetryWindow,
        bucket_seconds: int,
        fractions: Sequence[float],
        *,
        every: int = 1,
    ) -> dict[int, list[float]]:
        """Por inicio de cubo (como `BucketTotals.start_epoch`); sin los cubos vacíos."""
        ...

    async def route_percentiles(
        self,
        window: TelemetryWindow,
        fractions: Sequence[float],
        *,
        every: int = 1,
        routes: Sequence[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], list[float]]:
        """Por `(método, plantilla)`; solo las rutas de `routes` si se dan."""
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
