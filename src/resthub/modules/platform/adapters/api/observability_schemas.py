"""Contrato HTTP del panel de observabilidad."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from resthub.modules.platform.domain.observability import (
    KeysetPage,
    LogDetail,
    LogEntry,
    RequestEntry,
    RouteStats,
    StatusCount,
    Summary,
    Timeseries,
    Window,
)

LevelName = Literal["warning", "error"]
AccountKindName = Literal["staff", "platform", "preview", "anonymous"]


class ObservabilitySummaryResponse(BaseModel):
    window: Window
    requests: int
    errors_5xx: int
    errors_4xx: int
    # Fracción de 0 a 1 de peticiones con 5xx.
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    avg_db_ms: float
    active_restaurants: int
    # Filas que este proceso no pudo guardar desde que arrancó.
    dropped_events: int
    # `true` si los percentiles salen de una muestra (más de 200 000 peticiones).
    sampled: bool

    @classmethod
    def from_summary(cls, summary: Summary) -> ObservabilitySummaryResponse:
        return cls(
            window=summary.window,
            requests=summary.requests,
            errors_5xx=summary.errors_5xx,
            errors_4xx=summary.errors_4xx,
            error_rate=summary.error_rate,
            p50_ms=summary.p50_ms,
            p95_ms=summary.p95_ms,
            p99_ms=summary.p99_ms,
            avg_db_ms=summary.avg_db_ms,
            active_restaurants=summary.active_restaurants,
            dropped_events=summary.dropped_events,
            sampled=summary.sampled,
        )


class TimePointResponse(BaseModel):
    # Inicio del cubo, en UTC.
    t: datetime
    requests: int
    errors_5xx: int
    p95_ms: float


class TimeseriesResponse(BaseModel):
    bucket_seconds: int
    points: list[TimePointResponse]
    sampled: bool

    @classmethod
    def from_timeseries(cls, series: Timeseries) -> TimeseriesResponse:
        return cls(
            bucket_seconds=series.bucket_seconds,
            points=[
                TimePointResponse(
                    t=point.t,
                    requests=point.requests,
                    errors_5xx=point.errors_5xx,
                    p95_ms=point.p95_ms,
                )
                for point in series.points
            ],
            sampled=series.sampled,
        )


class RouteStatsResponse(BaseModel):
    method: str
    route: str
    requests: int
    errors_5xx: int
    p50_ms: float
    p95_ms: float
    avg_db_ms: float
    sampled: bool

    @classmethod
    def from_stats(cls, stats: RouteStats) -> RouteStatsResponse:
        return cls(
            method=stats.method,
            route=stats.route,
            requests=stats.requests,
            errors_5xx=stats.errors_5xx,
            p50_ms=stats.p50_ms,
            p95_ms=stats.p95_ms,
            avg_db_ms=stats.avg_db_ms,
            sampled=stats.sampled,
        )


class StatusCountResponse(BaseModel):
    status: int
    count: int

    @classmethod
    def from_count(cls, count: StatusCount) -> StatusCountResponse:
        return cls(status=count.status, count=count.count)


class LogEntryResponse(BaseModel):
    id: int
    at: datetime
    level: LevelName
    logger: str
    event: str
    request_id: str | None
    restaurant_id: int | None
    has_traceback: bool

    @classmethod
    def from_entry(cls, entry: LogEntry) -> LogEntryResponse:
        return cls(
            id=entry.id,
            at=entry.at,
            level="warning" if entry.level == "warning" else "error",
            logger=entry.logger,
            event=entry.event,
            request_id=entry.request_id,
            restaurant_id=entry.restaurant_id,
            has_traceback=entry.has_traceback,
        )


class LogPageResponse(BaseModel):
    items: list[LogEntryResponse]
    # Para la página siguiente: `?before_id=`. `null` en la última.
    next_before_id: int | None

    @classmethod
    def from_page(cls, page: KeysetPage[LogEntry]) -> LogPageResponse:
        return cls(
            items=[LogEntryResponse.from_entry(entry) for entry in page.items],
            next_before_id=page.next_before_id,
        )


class LogDetailResponse(LogEntryResponse):
    # Los campos del evento, ya sin datos sensibles.
    fields: dict[str, Any]
    traceback: str | None

    @classmethod
    def from_detail(cls, detail: LogDetail) -> LogDetailResponse:
        entry = LogEntryResponse.from_entry(detail.entry)
        return cls(**entry.model_dump(), fields=detail.fields, traceback=detail.traceback)


class RequestEntryResponse(BaseModel):
    id: int
    at: datetime
    method: str
    route: str
    status: int
    duration_ms: float
    db_ms: float
    db_queries: int
    request_id: str
    account_kind: AccountKindName
    restaurant_id: int | None
    account_id: int | None

    @classmethod
    def from_entry(cls, entry: RequestEntry) -> RequestEntryResponse:
        return cls(
            id=entry.id,
            at=entry.at,
            method=entry.method,
            route=entry.route,
            status=entry.status,
            duration_ms=entry.duration_ms,
            db_ms=entry.db_ms,
            db_queries=entry.db_queries,
            request_id=entry.request_id,
            account_kind=entry.account_kind,
            restaurant_id=entry.restaurant_id,
            account_id=entry.account_id,
        )


class RequestPageResponse(BaseModel):
    items: list[RequestEntryResponse]
    next_before_id: int | None

    @classmethod
    def from_page(cls, page: KeysetPage[RequestEntry]) -> RequestPageResponse:
        return cls(
            items=[RequestEntryResponse.from_entry(entry) for entry in page.items],
            next_before_id=page.next_before_id,
        )
