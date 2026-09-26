"""Lo que muestra el panel de observabilidad de la plataforma.

Cada caso de uso recibe el momento actual en vez de leer el reloj: la ventana
termina ahí, y las pruebas lo fijan para que los cubos no dependan de cuándo
corren.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from resthub.modules.platform.domain.exceptions import LogEntryNotFound
from resthub.modules.platform.domain.observability import (
    KeysetPage,
    LogDetail,
    LogEntry,
    RequestEntry,
    RouteSort,
    RouteStats,
    StatusCount,
    Summary,
    TelemetryWindow,
    TimePoint,
    Timeseries,
    Window,
    ratio,
    sampling_step,
)
from resthub.modules.platform.ports.observability import LogQuery, RequestQuery, TelemetryReader


def _page[T: (LogEntry, RequestEntry)](rows: Sequence[T], limit: int) -> KeysetPage[T]:
    # Se pidió una fila de más: si llegó, hay página siguiente y empieza antes
    # de la última que se muestra.
    items = list(rows[:limit])
    next_before_id = items[-1].id if len(rows) > limit and items else None
    return KeysetPage(items=items, next_before_id=next_before_id)


class ReadSummary:
    def __init__(self, reader: TelemetryReader) -> None:
        self._reader = reader

    async def __call__(
        self, window: Window, now: datetime, restaurant_id: int | None, dropped_events: int
    ) -> Summary:
        span = TelemetryWindow.ending_at(window, now, restaurant_id)
        totals = await self._reader.request_totals(span)
        every = sampling_step(totals.requests)
        p50, p95, p99 = await self._reader.duration_percentiles(
            span, (0.50, 0.95, 0.99), every=every
        )
        return Summary(
            window=window,
            requests=totals.requests,
            errors_5xx=totals.errors_5xx,
            errors_4xx=totals.errors_4xx,
            error_rate=ratio(totals.errors_5xx, totals.requests),
            p50_ms=p50,
            p95_ms=p95,
            p99_ms=p99,
            avg_db_ms=round(totals.avg_db_ms, 1),
            active_restaurants=totals.active_restaurants,
            dropped_events=dropped_events,
            sampled=every > 1,
        )


class ReadTimeseries:
    def __init__(self, reader: TelemetryReader) -> None:
        self._reader = reader

    async def __call__(
        self, window: Window, now: datetime, restaurant_id: int | None
    ) -> Timeseries:
        span = TelemetryWindow.ending_at(window, now, restaurant_id)
        width = window.bucket_seconds
        totals = await self._reader.request_totals(span)
        every = sampling_step(totals.requests)
        counts = {
            bucket.start_epoch: bucket for bucket in await self._reader.bucket_totals(span, width)
        }
        p95 = await self._reader.bucket_percentiles(span, width, (0.95,), every=every)

        # Cubos alineados a múltiplos de su ancho (en UTC), desde el que
        # contiene el inicio de la ventana hasta el que contiene `now`. Los
        # vacíos van con cero para que el gráfico no salte huecos.
        first = _bucket_start(span.since, width)
        last = _bucket_start(span.until, width)
        points = []
        for start in range(first, last + 1, width):
            bucket = counts.get(start)
            points.append(
                TimePoint(
                    t=datetime.fromtimestamp(start, UTC),
                    requests=bucket.requests if bucket else 0,
                    errors_5xx=bucket.errors_5xx if bucket else 0,
                    p95_ms=p95[start][0] if start in p95 else 0.0,
                )
            )
        return Timeseries(bucket_seconds=width, points=points, sampled=every > 1)


def _bucket_start(moment: datetime, width: int) -> int:
    return math.floor(moment.timestamp() / width) * width


_ROUTE_ORDER = {
    RouteSort.REQUESTS: lambda stats: (-stats.requests, stats.route, stats.method),
    RouteSort.P95: lambda stats: (-stats.p95_ms, -stats.requests, stats.route, stats.method),
    RouteSort.ERRORS: lambda stats: (-stats.errors_5xx, -stats.requests, stats.route, stats.method),
}


_ROUTE_FRACTIONS = (0.50, 0.95)


class ReadRoutes:
    def __init__(self, reader: TelemetryReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        window: Window,
        now: datetime,
        restaurant_id: int | None,
        sort: RouteSort,
        limit: int,
    ) -> list[RouteStats]:
        span = TelemetryWindow.ending_at(window, now, restaurant_id)
        totals = await self._reader.route_totals(span)
        every = sampling_step(sum(route.requests for route in totals))
        percentiles = await self._reader.route_percentiles(span, _ROUTE_FRACTIONS, every=every)
        missing = [
            (route.method, route.route)
            for route in totals
            if (route.method, route.route) not in percentiles
        ]
        if missing and every > 1:
            # Una ruta con menos filas que el paso del muestreo puede no tener
            # ninguna en la muestra; las suyas son pocas y se traen todas, las
            # de todas esas rutas en una sola consulta.
            percentiles.update(
                await self._reader.route_percentiles(span, _ROUTE_FRACTIONS, routes=missing)
            )

        stats = []
        for route in totals:
            p50, p95 = percentiles.get((route.method, route.route), (0.0, 0.0))
            stats.append(
                RouteStats(
                    method=route.method,
                    route=route.route,
                    requests=route.requests,
                    errors_5xx=route.errors_5xx,
                    p50_ms=p50,
                    p95_ms=p95,
                    avg_db_ms=round(route.avg_db_ms, 1),
                    sampled=every > 1,
                )
            )
        stats.sort(key=_ROUTE_ORDER[sort])
        return stats[:limit]


class ReadStatusCounts:
    def __init__(self, reader: TelemetryReader) -> None:
        self._reader = reader

    async def __call__(
        self, window: Window, now: datetime, restaurant_id: int | None
    ) -> list[StatusCount]:
        return await self._reader.status_counts(
            TelemetryWindow.ending_at(window, now, restaurant_id)
        )


class SearchLogs:
    def __init__(self, reader: TelemetryReader) -> None:
        self._reader = reader

    async def __call__(self, query: LogQuery) -> KeysetPage[LogEntry]:
        rows = await self._reader.logs(replace(query, limit=query.limit + 1))
        return _page(rows, query.limit)


class ReadLog:
    def __init__(self, reader: TelemetryReader) -> None:
        self._reader = reader

    async def __call__(self, entry_id: int) -> LogDetail:
        detail = await self._reader.log(entry_id)
        if detail is None:
            raise LogEntryNotFound(entry_id)
        return detail


class SearchRequests:
    def __init__(self, reader: TelemetryReader) -> None:
        self._reader = reader

    async def __call__(self, query: RequestQuery) -> KeysetPage[RequestEntry]:
        rows = await self._reader.requests(replace(query, limit=query.limit + 1))
        return _page(rows, query.limit)
