"""Lectura de la telemetría guardada, para el panel de observabilidad."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Integer,
    Select,
    and_,
    case,
    cast,
    distinct,
    func,
    literal_column,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.timestamps import as_utc
from resthub.modules.platform.adapters.persistence.models import ObsEventRow, ObsRequestRow
from resthub.modules.platform.domain.observability import (
    LogDetail,
    LogEntry,
    LogLevel,
    RequestEntry,
    StatusCount,
    TelemetryWindow,
)
from resthub.modules.platform.ports.observability import (
    BucketTotals,
    LogQuery,
    RequestQuery,
    RequestTotals,
    RouteTotals,
)

_SERVER_ERROR = 500
_CLIENT_ERROR = 400
_LIKE_ESCAPE = "\\"


def _escape_like(text: str) -> str:
    return text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")


def _parse_fields(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class SqlTelemetryReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Peticiones ------------------------------------------------------------

    @staticmethod
    def _in_window(window: TelemetryWindow) -> list[ColumnElement[bool]]:
        conditions = [ObsRequestRow.at >= window.since, ObsRequestRow.at <= window.until]
        if window.restaurant_id is not None:
            conditions.append(ObsRequestRow.restaurant_id == window.restaurant_id)
        return conditions

    async def request_totals(self, window: TelemetryWindow) -> RequestTotals:
        row = (
            await self._session.execute(
                select(
                    func.count(),
                    func.sum(case((ObsRequestRow.status >= _SERVER_ERROR, 1), else_=0)),
                    func.sum(
                        case(
                            (
                                and_(
                                    ObsRequestRow.status >= _CLIENT_ERROR,
                                    ObsRequestRow.status < _SERVER_ERROR,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.avg(ObsRequestRow.db_ms),
                    func.count(distinct(ObsRequestRow.restaurant_id)),
                ).where(*self._in_window(window))
            )
        ).one()
        return RequestTotals(
            requests=int(row[0] or 0),
            errors_5xx=int(row[1] or 0),
            errors_4xx=int(row[2] or 0),
            avg_db_ms=float(row[3] or 0.0),
            active_restaurants=int(row[4] or 0),
        )

    def _sampled[T: tuple[Any, ...]](self, statement: Select[T], every: int) -> Select[T]:
        # Una de cada `every` por identificador: los identificadores crecen con
        # el tiempo, así que la muestra cubre la ventana de forma pareja.
        return statement.where(ObsRequestRow.id % every == 0) if every > 1 else statement

    async def durations(
        self, window: TelemetryWindow, *, every: int = 1, route: tuple[str, str] | None = None
    ) -> list[float]:
        statement = select(ObsRequestRow.duration_ms).where(*self._in_window(window))
        if route is not None:
            method, template = route
            statement = statement.where(
                ObsRequestRow.method == method, ObsRequestRow.route == template
            )
        result = await self._session.execute(self._sampled(statement, every))
        return [float(value) for value in result.scalars()]

    async def timed_durations(
        self, window: TelemetryWindow, *, every: int = 1
    ) -> list[tuple[datetime, float]]:
        statement = select(ObsRequestRow.at, ObsRequestRow.duration_ms).where(
            *self._in_window(window)
        )
        result = await self._session.execute(self._sampled(statement, every))
        return [(as_utc(at), float(duration)) for at, duration in result.tuples()]

    async def route_durations(
        self, window: TelemetryWindow, *, every: int = 1
    ) -> list[tuple[str, str, float]]:
        statement = select(
            ObsRequestRow.method, ObsRequestRow.route, ObsRequestRow.duration_ms
        ).where(*self._in_window(window))
        result = await self._session.execute(self._sampled(statement, every))
        return [(method, route, float(duration)) for method, route, duration in result.tuples()]

    def _epoch_seconds(self) -> ColumnElement[int]:
        # Segundos desde 1970 del momento de la petición, en cada base a su
        # manera. SQLite guarda la fecha como texto en UTC.
        if self._session.get_bind().dialect.name == "sqlite":
            return cast(func.strftime("%s", ObsRequestRow.at), Integer)
        return cast(func.floor(func.extract("epoch", ObsRequestRow.at)), BigInteger)

    async def bucket_totals(
        self, window: TelemetryWindow, bucket_seconds: int
    ) -> list[BucketTotals]:
        # El ancho va como literal y no como parámetro: PostgreSQL no reconoce
        # como la misma expresión un `GROUP BY` con otro parámetro que el `SELECT`.
        width = literal_column(str(int(bucket_seconds)), Integer)
        bucket = ((self._epoch_seconds() // width) * width).label("bucket")
        result = await self._session.execute(
            select(
                bucket,
                func.count(),
                func.sum(case((ObsRequestRow.status >= _SERVER_ERROR, 1), else_=0)),
            )
            .where(*self._in_window(window))
            .group_by(bucket)
        )
        return [
            BucketTotals(start_epoch=int(start), requests=int(count), errors_5xx=int(errors or 0))
            for start, count, errors in result.tuples()
        ]

    async def route_totals(self, window: TelemetryWindow) -> list[RouteTotals]:
        result = await self._session.execute(
            select(
                ObsRequestRow.method,
                ObsRequestRow.route,
                func.count(),
                func.sum(case((ObsRequestRow.status >= _SERVER_ERROR, 1), else_=0)),
                func.avg(ObsRequestRow.db_ms),
            )
            .where(*self._in_window(window))
            .group_by(ObsRequestRow.method, ObsRequestRow.route)
        )
        return [
            RouteTotals(
                method=method,
                route=route,
                requests=int(count),
                errors_5xx=int(errors or 0),
                avg_db_ms=float(avg_db or 0.0),
            )
            for method, route, count, errors, avg_db in result.tuples()
        ]

    async def status_counts(self, window: TelemetryWindow) -> list[StatusCount]:
        result = await self._session.execute(
            select(ObsRequestRow.status, func.count())
            .where(*self._in_window(window))
            .group_by(ObsRequestRow.status)
            .order_by(ObsRequestRow.status)
        )
        return [StatusCount(status=int(code), count=int(count)) for code, count in result.tuples()]

    async def requests(self, query: RequestQuery) -> list[RequestEntry]:
        statement = select(ObsRequestRow).where(*self._in_window(query.window))
        if query.status_min is not None:
            statement = statement.where(ObsRequestRow.status >= query.status_min)
        if query.route is not None:
            statement = statement.where(ObsRequestRow.route == query.route)
        if query.request_id is not None:
            statement = statement.where(ObsRequestRow.request_id == query.request_id)
        if query.before_id is not None:
            statement = statement.where(ObsRequestRow.id < query.before_id)
        result = await self._session.execute(
            statement.order_by(ObsRequestRow.id.desc()).limit(query.limit)
        )
        return [
            RequestEntry(
                id=row.id,
                at=as_utc(row.at),
                method=row.method,
                route=row.route,
                status=row.status,
                duration_ms=row.duration_ms,
                db_ms=row.db_ms,
                db_queries=row.db_queries,
                request_id=row.request_id,
                account_kind=row.account_kind,
                restaurant_id=row.restaurant_id,
                account_id=row.account_id,
            )
            for row in result.scalars()
        ]

    # --- Eventos ---------------------------------------------------------------

    async def logs(self, query: LogQuery) -> list[LogEntry]:
        window = query.window
        statement = select(ObsEventRow).where(
            ObsEventRow.at >= window.since, ObsEventRow.at <= window.until
        )
        if window.restaurant_id is not None:
            statement = statement.where(ObsEventRow.restaurant_id == window.restaurant_id)
        if query.level is not None:
            statement = statement.where(ObsEventRow.level == LogLevel(query.level).value)
        if query.request_id is not None:
            statement = statement.where(ObsEventRow.request_id == query.request_id)
        if query.before_id is not None:
            statement = statement.where(ObsEventRow.id < query.before_id)
        if query.search:
            pattern = f"%{_escape_like(query.search)}%"
            statement = statement.where(
                or_(
                    ObsEventRow.event.ilike(pattern, escape=_LIKE_ESCAPE),
                    ObsEventRow.fields.ilike(pattern, escape=_LIKE_ESCAPE),
                )
            )
        # Sin traer `fields` ni `traceback`: la lista no los muestra y pesan.
        statement = statement.with_only_columns(
            ObsEventRow.id,
            ObsEventRow.at,
            ObsEventRow.level,
            ObsEventRow.logger,
            ObsEventRow.event,
            ObsEventRow.request_id,
            ObsEventRow.restaurant_id,
            ObsEventRow.traceback.is_not(None),
        )
        result = await self._session.execute(
            statement.order_by(ObsEventRow.id.desc()).limit(query.limit)
        )
        return [
            LogEntry(
                id=entry_id,
                at=as_utc(at),
                level=level,
                logger=logger,
                event=event,
                request_id=request_id,
                restaurant_id=restaurant_id,
                has_traceback=bool(has_traceback),
            )
            for (
                entry_id,
                at,
                level,
                logger,
                event,
                request_id,
                restaurant_id,
                has_traceback,
            ) in result.tuples()
        ]

    async def log(self, entry_id: int) -> LogDetail | None:
        row = await self._session.get(ObsEventRow, entry_id)
        if row is None:
            return None
        return LogDetail(
            entry=LogEntry(
                id=row.id,
                at=as_utc(row.at),
                level=row.level,
                logger=row.logger,
                event=row.event,
                request_id=row.request_id,
                restaurant_id=row.restaurant_id,
                has_traceback=row.traceback is not None,
            ),
            fields=_parse_fields(row.fields),
            traceback=row.traceback,
        )
