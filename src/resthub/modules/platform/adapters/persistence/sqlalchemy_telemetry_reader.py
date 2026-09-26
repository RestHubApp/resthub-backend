"""Lectura de la telemetría guardada, para el panel de observabilidad.

Los percentiles los calcula PostgreSQL con `percentile_cont`, sin traer las
duraciones. SQLite, la base de las pruebas, no lo tiene: ahí se traen las
duraciones y se calculan en Python con `domain.observability.percentile`, que
interpola igual que `percentile_cont`. Las dos ramas usan la misma muestra.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
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
    tuple_,
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
    percentile,
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


def _sampled[T: tuple[Any, ...]](statement: Select[T], every: int) -> Select[T]:
    # Una de cada `every` por identificador: los identificadores crecen con
    # el tiempo, así que la muestra cubre la ventana de forma pareja.
    return statement.where(ObsRequestRow.id % every == 0) if every > 1 else statement


def percentile_statement(
    keys: Sequence[ColumnElement[Any]],
    conditions: Sequence[ColumnElement[bool]],
    fractions: Sequence[float],
    every: int,
) -> Select[Any]:
    """Los percentiles de la duración por grupo, con `percentile_cont` de PostgreSQL."""
    duration = ObsRequestRow.duration_ms
    statement = _sampled(
        select(
            *keys,
            *(func.percentile_cont(fraction).within_group(duration) for fraction in fractions),
        ).where(*conditions),
        every,
    )
    return statement.group_by(*keys) if keys else statement


def _rounded(values: Sequence[Any]) -> list[float]:
    return [0.0 if value is None else round(float(value), 1) for value in values]


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

    def _dialect(self) -> str:
        return self._session.get_bind().dialect.name

    async def _percentiles(
        self,
        keys: Sequence[ColumnElement[Any]],
        conditions: Sequence[ColumnElement[bool]],
        fractions: Sequence[float],
        every: int,
    ) -> dict[tuple[Any, ...], list[float]]:
        """Los percentiles de cada grupo de `keys`; sin `keys`, un solo grupo `()`."""
        width = len(keys)
        if self._dialect() == "postgresql":
            result = await self._session.execute(
                percentile_statement(keys, conditions, fractions, every)
            )
            return {tuple(row[:width]): _rounded(row[width:]) for row in result}
        # Se agrupa mientras se recorre el resultado, sin armar antes una lista
        # con una tupla por petición.
        statement = _sampled(select(*keys, ObsRequestRow.duration_ms).where(*conditions), every)
        groups: defaultdict[tuple[Any, ...], list[float]] = defaultdict(list)
        for row in await self._session.execute(statement):
            groups[tuple(row[:width])].append(float(row[width]))
        percentiles = {}
        for key, values in groups.items():
            values.sort()
            percentiles[key] = [percentile(values, fraction) for fraction in fractions]
        return percentiles

    async def duration_percentiles(
        self, window: TelemetryWindow, fractions: Sequence[float], *, every: int = 1
    ) -> list[float]:
        found = await self._percentiles((), self._in_window(window), fractions, every)
        return found.get((), [0.0] * len(fractions))

    async def bucket_percentiles(
        self,
        window: TelemetryWindow,
        bucket_seconds: int,
        fractions: Sequence[float],
        *,
        every: int = 1,
    ) -> dict[int, list[float]]:
        found = await self._percentiles(
            (self._bucket(bucket_seconds),), self._in_window(window), fractions, every
        )
        return {int(start): values for (start,), values in found.items()}

    async def route_percentiles(
        self,
        window: TelemetryWindow,
        fractions: Sequence[float],
        *,
        every: int = 1,
        routes: Sequence[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], list[float]]:
        conditions = self._in_window(window)
        if routes is not None:
            if not routes:
                return {}
            conditions.append(tuple_(ObsRequestRow.method, ObsRequestRow.route).in_(routes))
        found = await self._percentiles(
            (ObsRequestRow.method, ObsRequestRow.route), conditions, fractions, every
        )
        return {(str(method), str(route)): values for (method, route), values in found.items()}

    def _epoch_seconds(self) -> ColumnElement[int]:
        # Segundos desde 1970 del momento de la petición, en cada base a su
        # manera. SQLite guarda la fecha como texto en UTC.
        if self._dialect() == "sqlite":
            return cast(func.strftime("%s", ObsRequestRow.at), Integer)
        return cast(func.floor(func.extract("epoch", ObsRequestRow.at)), BigInteger)

    def _bucket(self, bucket_seconds: int) -> ColumnElement[int]:
        # El ancho va como literal y no como parámetro: PostgreSQL no reconoce
        # como la misma expresión un `GROUP BY` con otro parámetro que el `SELECT`.
        width = literal_column(str(int(bucket_seconds)), Integer)
        return ((self._epoch_seconds() // width) * width).label("bucket")

    async def bucket_totals(
        self, window: TelemetryWindow, bucket_seconds: int
    ) -> list[BucketTotals]:
        bucket = self._bucket(bucket_seconds)
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
