"""Adaptador de entrada HTTP de los indicadores.

Todo exige `insights.read`, que hoy tiene solo el encargado, y mira solo el
restaurante de quien pregunta. Los reportes aceptan `date_from` y `date_to`,
días del restaurante con los dos extremos incluidos; sin ellos, los últimos
treinta días hasta hoy.

Hay dos clases de endpoint:

- Reportes deterministas (ventas, platos, pagos, personal, mermas, stock):
  cuentas sobre datos ya registrados. Nunca llaman a la IA.
- Decisiones (reposición, notas de pedido, causas de merma): las toma Jev si
  está configurado, o las reglas fijas. Consultarlas no llama a la IA;
  actualizarlas (los `POST`) sí, y guarda cada decisión para auditarla.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.core.realtime_broker import EventPublisherDep
from resthub.modules.insights.adapters.api.dependencies import (
    DecisionEngineDep,
    DecisionLogDep,
    KitchenNotesDirectoryDep,
    RestaurantCalendarDep,
    SalesDirectoryDep,
    StockDirectoryDep,
    SubjectDirectoryDep,
)
from resthub.modules.insights.adapters.api.schemas import (
    AiDecisionPageResponse,
    ClassifyNotesResponse,
    ClassifyWasteResponse,
    DailySalesResponse,
    DishMarginsResponse,
    HourlySalesResponse,
    LowStockResponse,
    NoteClassificationResponse,
    OrderNotesResponse,
    PaymentMixResponse,
    RestockResponse,
    SalesSummaryResponse,
    TopDishesResponse,
    WaitersResponse,
    WasteReportResponse,
)
from resthub.modules.insights.domain.decisions import DecisionKind, Engine, SubjectType
from resthub.modules.insights.domain.exceptions import InsightsError
from resthub.modules.insights.ports.decision_log import DecisionQuery
from resthub.modules.insights.use_cases.decision_audit import ListDecisions
from resthub.modules.insights.use_cases.order_notes import (
    ClassifyActiveNotes,
    ClassifyKitchenNotes,
    ReadNoteClassifications,
)
from resthub.modules.insights.use_cases.restock import ReadRestock, RefreshRestock
from resthub.modules.insights.use_cases.sales_reports import (
    DEFAULT_TOP_DISHES,
    ReadDailySales,
    ReadDishMargins,
    ReadHourlySales,
    ReadPaymentMix,
    ReadSalesSummary,
    ReadTopDishes,
    ReadWaiterPerformance,
)
from resthub.modules.insights.use_cases.shared import PeriodQuery
from resthub.modules.insights.use_cases.stock_reports import ReadLowStock, ReadWasteReport
from resthub.modules.insights.use_cases.waste_causes import ClassifyPendingWaste

router = APIRouter()

InsightsReaderDep = Annotated[Principal, Depends(require_permission(Permission.INSIGHTS_READ))]
DateFrom = Annotated[date | None, Query(description="Desde este día del local (incluido)")]
DateTo = Annotated[date | None, Query(description="Hasta este día del local (incluido)")]

# Un tablero no muestra más pedidos que esto a la vez.
MAX_ORDER_IDS = 100


def _http_error(error: InsightsError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


def _query(principal: Principal, date_from: date | None, date_to: date | None) -> PeriodQuery:
    return PeriodQuery(restaurant_id=principal.restaurant_id, date_from=date_from, date_to=date_to)


# -- Ventas ------------------------------------------------------------------


@router.get(
    "/summary",
    response_model=SalesSummaryResponse,
    summary="Ventas, pedidos, ticket promedio y cancelados, contra el período anterior",
)
async def sales_summary(
    principal: InsightsReaderDep,
    calendar: RestaurantCalendarDep,
    sales: SalesDirectoryDep,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> SalesSummaryResponse:
    try:
        report = await ReadSalesSummary(calendar, sales)(_query(principal, date_from, date_to))
    except InsightsError as error:
        raise _http_error(error) from error
    return SalesSummaryResponse.build(report)


@router.get("/sales/daily", response_model=DailySalesResponse, summary="Ventas por día")
async def daily_sales(
    principal: InsightsReaderDep,
    calendar: RestaurantCalendarDep,
    sales: SalesDirectoryDep,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> DailySalesResponse:
    try:
        report = await ReadDailySales(calendar, sales)(_query(principal, date_from, date_to))
    except InsightsError as error:
        raise _http_error(error) from error
    return DailySalesResponse.build(report)


@router.get(
    "/sales/hourly",
    response_model=HourlySalesResponse,
    summary="Ventas por día de la semana y hora (mapa de calor)",
)
async def hourly_sales(
    principal: InsightsReaderDep,
    calendar: RestaurantCalendarDep,
    sales: SalesDirectoryDep,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> HourlySalesResponse:
    try:
        report = await ReadHourlySales(calendar, sales)(_query(principal, date_from, date_to))
    except InsightsError as error:
        raise _http_error(error) from error
    return HourlySalesResponse.build(report)


@router.get("/payments", response_model=PaymentMixResponse, summary="Ingresos por medio de pago")
async def payment_mix(
    principal: InsightsReaderDep,
    calendar: RestaurantCalendarDep,
    sales: SalesDirectoryDep,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> PaymentMixResponse:
    try:
        report = await ReadPaymentMix(calendar, sales)(_query(principal, date_from, date_to))
    except InsightsError as error:
        raise _http_error(error) from error
    return PaymentMixResponse.build(report)


@router.get("/waiters", response_model=WaitersResponse, summary="Pedidos y ventas por mesero")
async def waiter_performance(
    principal: InsightsReaderDep,
    calendar: RestaurantCalendarDep,
    sales: SalesDirectoryDep,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> WaitersResponse:
    try:
        report = await ReadWaiterPerformance(calendar, sales)(_query(principal, date_from, date_to))
    except InsightsError as error:
        raise _http_error(error) from error
    return WaitersResponse.build(report)


@router.get("/dishes/top", response_model=TopDishesResponse, summary="Platos más vendidos")
async def top_dishes(
    principal: InsightsReaderDep,
    calendar: RestaurantCalendarDep,
    sales: SalesDirectoryDep,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    limit: Annotated[int, Query(ge=1, le=50)] = DEFAULT_TOP_DISHES,
) -> TopDishesResponse:
    try:
        report = await ReadTopDishes(calendar, sales)(
            _query(principal, date_from, date_to), limit=limit
        )
    except InsightsError as error:
        raise _http_error(error) from error
    return TopDishesResponse.build(report)


@router.get(
    "/dishes/margins",
    response_model=DishMarginsResponse,
    summary="Margen por plato: precio menos costo de receta, y lo que dejó en el rango",
)
async def dish_margins(
    principal: InsightsReaderDep,
    calendar: RestaurantCalendarDep,
    sales: SalesDirectoryDep,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> DishMarginsResponse:
    try:
        report = await ReadDishMargins(calendar, sales)(_query(principal, date_from, date_to))
    except InsightsError as error:
        raise _http_error(error) from error
    return DishMarginsResponse.build(report)


# -- Almacén -----------------------------------------------------------------


@router.get(
    "/low-stock",
    response_model=list[LowStockResponse],
    summary="Insumos bajo el mínimo, del más comprometido al menos",
)
async def low_stock(
    principal: InsightsReaderDep, stock: StockDirectoryDep
) -> list[LowStockResponse]:
    facts = await ReadLowStock(stock)(principal.restaurant_id)
    return [LowStockResponse.build(fact) for fact in facts]


@router.get(
    "/waste",
    response_model=WasteReportResponse,
    summary="Mermas del rango por insumo y por causa",
)
async def waste_report(
    principal: InsightsReaderDep,
    calendar: RestaurantCalendarDep,
    stock: StockDirectoryDep,
    decisions: DecisionLogDep,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> WasteReportResponse:
    try:
        report = await ReadWasteReport(calendar, stock, decisions)(
            _query(principal, date_from, date_to)
        )
    except InsightsError as error:
        raise _http_error(error) from error
    return WasteReportResponse.build(report)


@router.post(
    "/waste/classify",
    response_model=ClassifyWasteResponse,
    summary="Clasificar la causa de las mermas que todavía no la tienen",
)
async def classify_waste(
    principal: InsightsReaderDep,
    stock: StockDirectoryDep,
    decisions: DecisionLogDep,
    engine: DecisionEngineDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> ClassifyWasteResponse:
    run = await ClassifyPendingWaste(stock, decisions, engine, activity, events)(
        principal.restaurant_id, principal.user_id
    )
    return ClassifyWasteResponse.build(run)


# -- Reposición --------------------------------------------------------------


@router.get(
    "/restock",
    response_model=RestockResponse,
    summary="Sugerencia de compra por insumo (la última guardada, sin llamar a la IA)",
)
async def read_restock(
    principal: InsightsReaderDep, stock: StockDirectoryDep, decisions: DecisionLogDep
) -> RestockResponse:
    return RestockResponse.build(await ReadRestock(stock, decisions)(principal.restaurant_id))


@router.post(
    "/restock/refresh",
    response_model=RestockResponse,
    summary="Recalcular y decidir de nuevo la compra de cada insumo",
)
async def refresh_restock(
    principal: InsightsReaderDep,
    stock: StockDirectoryDep,
    decisions: DecisionLogDep,
    engine: DecisionEngineDep,
    activity: ActivityRecorderDep,
) -> RestockResponse:
    items = await RefreshRestock(stock, decisions, engine, activity)(
        principal.restaurant_id, principal.user_id
    )
    return RestockResponse.build(items)


# -- Notas de pedido ---------------------------------------------------------


@router.get(
    "/order-notes",
    response_model=OrderNotesResponse,
    summary="Clasificación de las notas de unos pedidos (alergias para el tablero)",
)
async def read_order_notes(
    principal: InsightsReaderDep,
    notes: KitchenNotesDirectoryDep,
    decisions: DecisionLogDep,
    order_ids: Annotated[
        list[int], Query(description="Pedidos a consultar (repetible)", max_length=MAX_ORDER_IDS)
    ],
) -> OrderNotesResponse:
    views = await ReadNoteClassifications(notes, decisions)(principal.restaurant_id, order_ids)
    return OrderNotesResponse(items=[NoteClassificationResponse.build(view) for view in views])


@router.post(
    "/order-notes/classify",
    response_model=ClassifyNotesResponse,
    summary="Clasificar las notas aún sin clasificar de los pedidos en curso",
)
async def classify_order_notes(
    principal: InsightsReaderDep,
    notes: KitchenNotesDirectoryDep,
    decisions: DecisionLogDep,
    engine: DecisionEngineDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> ClassifyNotesResponse:
    run = await ClassifyActiveNotes(
        notes, ClassifyKitchenNotes(decisions, engine, events), activity
    )(principal.restaurant_id, principal.user_id)
    return ClassifyNotesResponse.build(run)


# -- Auditoría ---------------------------------------------------------------


@router.get(
    "/ai-decisions",
    response_model=AiDecisionPageResponse,
    summary="Decisiones de la IA y de las reglas, de la más nueva a la más vieja",
)
async def list_ai_decisions(
    principal: InsightsReaderDep,
    decisions: DecisionLogDep,
    subjects: SubjectDirectoryDep,
    kind: DecisionKind | None = None,
    engine: Engine | None = None,
    subject_type: SubjectType | None = None,
    subject_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AiDecisionPageResponse:
    page = await ListDecisions(decisions, subjects)(
        DecisionQuery(
            restaurant_id=principal.restaurant_id,
            kind=kind,
            engine=engine,
            subject_type=subject_type,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    )
    return AiDecisionPageResponse.build(page)
