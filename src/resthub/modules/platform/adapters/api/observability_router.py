"""Panel de observabilidad: cómo anda la aplicación, desde la administración del sistema.

Todo exige una credencial de plataforma. Estas rutas no se capturan: mirar el
panel no llena el panel. El filtro `restaurant_id` viene del cliente a
propósito, como en el resto de `/platform`: la plataforma mira a todos los
locales.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from resthub.core.telemetry import TelemetryRecorder, get_telemetry
from resthub.modules.platform.adapters.api.dependencies import (
    CurrentAdminDep,
    ObservabilityClockDep,
    TelemetryReaderDep,
)
from resthub.modules.platform.adapters.api.errors import to_http
from resthub.modules.platform.adapters.api.observability_schemas import (
    LogDetailResponse,
    LogPageResponse,
    ObservabilitySummaryResponse,
    RequestPageResponse,
    RouteStatsResponse,
    StatusCountResponse,
    TimeseriesResponse,
)
from resthub.modules.platform.domain.exceptions import PlatformError
from resthub.modules.platform.domain.observability import (
    DEFAULT_PAGE_LIMIT,
    DEFAULT_ROUTES_LIMIT,
    MAX_PAGE_LIMIT,
    MAX_ROUTES_LIMIT,
    MAX_SEARCH_LENGTH,
    LogLevel,
    RouteSort,
    TelemetryWindow,
    Window,
)
from resthub.modules.platform.ports.observability import LogQuery, RequestQuery
from resthub.modules.platform.use_cases.observability import (
    ReadLog,
    ReadRoutes,
    ReadStatusCounts,
    ReadSummary,
    ReadTimeseries,
    SearchLogs,
    SearchRequests,
)

# Topes de las columnas: más allá la base respondería con un error de datos.
MAX_RESTAURANT_ID = 2**31 - 1
MAX_ENTRY_ID = 2**63 - 1
MAX_REQUEST_ID_LENGTH = 128
MAX_ROUTE_LENGTH = 255

WindowQuery = Annotated[Window, Query(description="Ventana que termina ahora.")]
RestaurantFilter = Annotated[
    int | None, Query(ge=1, le=MAX_RESTAURANT_ID, description="Solo este restaurante.")
]
BeforeIdQuery = Annotated[
    int | None, Query(ge=1, le=MAX_ENTRY_ID, description="`next_before_id` de la página anterior.")
]
RequestIdQuery = Annotated[str | None, Query(min_length=1, max_length=MAX_REQUEST_ID_LENGTH)]
PageLimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
TelemetryDep = Annotated[TelemetryRecorder, Depends(get_telemetry)]

router = APIRouter()


@router.get(
    "/summary", response_model=ObservabilitySummaryResponse, summary="Indicadores de la ventana"
)
async def read_summary(
    _: CurrentAdminDep,
    reader: TelemetryReaderDep,
    clock: ObservabilityClockDep,
    telemetry: TelemetryDep,
    window: WindowQuery = Window.DAY,
    restaurant_id: RestaurantFilter = None,
) -> ObservabilitySummaryResponse:
    summary = await ReadSummary(reader)(window, clock(), restaurant_id, telemetry.dropped)
    return ObservabilitySummaryResponse.from_summary(summary)


@router.get("/timeseries", response_model=TimeseriesResponse, summary="Serie temporal")
async def read_timeseries(
    _: CurrentAdminDep,
    reader: TelemetryReaderDep,
    clock: ObservabilityClockDep,
    window: WindowQuery = Window.DAY,
    restaurant_id: RestaurantFilter = None,
) -> TimeseriesResponse:
    series = await ReadTimeseries(reader)(window, clock(), restaurant_id)
    return TimeseriesResponse.from_timeseries(series)


@router.get("/routes", response_model=list[RouteStatsResponse], summary="Rutas de la ventana")
async def read_routes(
    _: CurrentAdminDep,
    reader: TelemetryReaderDep,
    clock: ObservabilityClockDep,
    window: WindowQuery = Window.DAY,
    restaurant_id: RestaurantFilter = None,
    sort: RouteSort = RouteSort.REQUESTS,
    limit: Annotated[int, Query(ge=1, le=MAX_ROUTES_LIMIT)] = DEFAULT_ROUTES_LIMIT,
) -> list[RouteStatsResponse]:
    routes = await ReadRoutes(reader)(window, clock(), restaurant_id, sort, limit)
    return [RouteStatsResponse.from_stats(stats) for stats in routes]


@router.get(
    "/status", response_model=list[StatusCountResponse], summary="Peticiones por estado HTTP"
)
async def read_status(
    _: CurrentAdminDep,
    reader: TelemetryReaderDep,
    clock: ObservabilityClockDep,
    window: WindowQuery = Window.DAY,
    restaurant_id: RestaurantFilter = None,
) -> list[StatusCountResponse]:
    counts = await ReadStatusCounts(reader)(window, clock(), restaurant_id)
    return [StatusCountResponse.from_count(count) for count in counts]


@router.get("/logs", response_model=LogPageResponse, summary="Buscar en los logs")
async def search_logs(
    _: CurrentAdminDep,
    reader: TelemetryReaderDep,
    clock: ObservabilityClockDep,
    window: WindowQuery = Window.DAY,
    restaurant_id: RestaurantFilter = None,
    level: LogLevel | None = None,
    search: Annotated[str | None, Query(max_length=MAX_SEARCH_LENGTH)] = None,
    request_id: RequestIdQuery = None,
    limit: PageLimitQuery = DEFAULT_PAGE_LIMIT,
    before_id: BeforeIdQuery = None,
) -> LogPageResponse:
    query = LogQuery(
        window=TelemetryWindow.ending_at(window, clock(), restaurant_id),
        limit=limit,
        level=level,
        search=(search or "").strip() or None,
        request_id=request_id,
        before_id=before_id,
    )
    return LogPageResponse.from_page(await SearchLogs(reader)(query))


@router.get("/logs/{entry_id}", response_model=LogDetailResponse, summary="Una entrada de log")
async def read_log(
    _: CurrentAdminDep,
    reader: TelemetryReaderDep,
    entry_id: Annotated[int, Path(ge=1, le=MAX_ENTRY_ID)],
) -> LogDetailResponse:
    try:
        detail = await ReadLog(reader)(entry_id)
    except PlatformError as error:
        raise to_http(error) from error
    return LogDetailResponse.from_detail(detail)


@router.get("/requests", response_model=RequestPageResponse, summary="Peticiones una por una")
async def search_requests(
    _: CurrentAdminDep,
    reader: TelemetryReaderDep,
    clock: ObservabilityClockDep,
    window: WindowQuery = Window.DAY,
    restaurant_id: RestaurantFilter = None,
    status_min: Annotated[int | None, Query(ge=100, le=599)] = None,
    route: Annotated[
        str | None, Query(min_length=1, max_length=MAX_ROUTE_LENGTH, description="Plantilla.")
    ] = None,
    request_id: RequestIdQuery = None,
    limit: PageLimitQuery = DEFAULT_PAGE_LIMIT,
    before_id: BeforeIdQuery = None,
) -> RequestPageResponse:
    query = RequestQuery(
        window=TelemetryWindow.ending_at(window, clock(), restaurant_id),
        limit=limit,
        status_min=status_min,
        route=route,
        request_id=request_id,
        before_id=before_id,
    )
    return RequestPageResponse.from_page(await SearchRequests(reader)(query))
