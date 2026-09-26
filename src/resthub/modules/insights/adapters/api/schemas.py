"""Contrato HTTP de los indicadores.

Montos y cantidades viajan como `Decimal`, que en JSON se escribe como texto,
igual que en el resto del API. Los porcentajes van de 0 a 100 con un decimal.
La confianza de la IA va de 0 a 1 como número. Cada código trae su etiqueta en
castellano (`*_label`) para que el panel no tenga que traducir nada.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from resthub.core.pagination import Page
from resthub.modules.insights.domain.decisions import (
    ConfidenceKind,
    DecisionKind,
    Engine,
    FallbackReason,
    NoteType,
    RestockAction,
    SubjectType,
    WasteCause,
    confidence_kind,
)
from resthub.modules.insights.domain.period import DateRange
from resthub.modules.insights.domain.sales import (
    DailySales,
    DishMargin,
    HourlyCell,
    PaymentShare,
    SalesSummary,
    SoldDish,
    WaiterPerformance,
    change_percent,
)
from resthub.modules.insights.domain.stock import StockFact, WasteReport
from resthub.modules.insights.use_cases.decision_audit import DecisionEntry
from resthub.modules.insights.use_cases.order_notes import ClassificationRun, NoteView
from resthub.modules.insights.use_cases.restock import RestockItem
from resthub.modules.insights.use_cases.sales_reports import SummaryReport
from resthub.modules.insights.use_cases.shared import Report
from resthub.modules.insights.use_cases.waste_causes import WasteClassificationRun


class PeriodResponse(BaseModel):
    date_from: date
    date_to: date
    days: int
    # La zona del restaurante en que se contaron los días y las horas.
    timezone: str

    @classmethod
    def build(cls, period: DateRange, timezone: str) -> PeriodResponse:
        return cls(date_from=period.start, date_to=period.end, days=period.days, timezone=timezone)


def _period(report: Report[Any]) -> PeriodResponse:
    return PeriodResponse.build(report.period, report.timezone)


# -- Ventas ------------------------------------------------------------------


class SalesTotals(BaseModel):
    sales: Decimal
    paid_orders: int
    average_ticket: Decimal
    cancelled_orders: int
    cancelled_amount: Decimal

    @classmethod
    def build(cls, summary: SalesSummary) -> SalesTotals:
        return cls(
            sales=summary.sales,
            paid_orders=summary.paid_orders,
            average_ticket=summary.average_ticket,
            cancelled_orders=summary.cancelled_orders,
            cancelled_amount=summary.cancelled_amount,
        )


class PreviousTotals(SalesTotals):
    date_from: date
    date_to: date


class SalesSummaryResponse(SalesTotals):
    period: PeriodResponse
    # El rango del mismo largo justo anterior.
    previous: PreviousTotals
    # Variación contra ese rango; `null` si antes no hubo ventas.
    sales_change_percent: Decimal | None
    paid_orders_change_percent: Decimal | None
    average_ticket_change_percent: Decimal | None

    @classmethod
    def build(cls, report: Report[SummaryReport]) -> SalesSummaryResponse:
        current, previous = report.data.current, report.data.previous
        return cls(
            **SalesTotals.build(current).model_dump(),
            period=_period(report),
            previous=PreviousTotals(
                **SalesTotals.build(previous).model_dump(),
                date_from=report.data.previous_period.start,
                date_to=report.data.previous_period.end,
            ),
            sales_change_percent=change_percent(current.sales, previous.sales),
            paid_orders_change_percent=change_percent(
                Decimal(current.paid_orders), Decimal(previous.paid_orders)
            ),
            average_ticket_change_percent=change_percent(
                current.average_ticket, previous.average_ticket
            ),
        )


class DailySalesPoint(BaseModel):
    date: date
    sales: Decimal
    paid_orders: int
    average_ticket: Decimal

    @classmethod
    def build(cls, point: DailySales) -> DailySalesPoint:
        return cls(
            date=point.day,
            sales=point.sales,
            paid_orders=point.paid_orders,
            average_ticket=point.average_ticket,
        )


class DailySalesResponse(BaseModel):
    period: PeriodResponse
    # Un punto por día del rango, también los días sin ventas (en cero).
    days: list[DailySalesPoint]

    @classmethod
    def build(cls, report: Report[list[DailySales]]) -> DailySalesResponse:
        return cls(period=_period(report), days=[DailySalesPoint.build(p) for p in report.data])


class HourlyCellResponse(BaseModel):
    # 0 es lunes y 6 domingo.
    weekday: int
    weekday_label: str
    hour: int
    paid_orders: int
    sales: Decimal
    # Venta de esa hora en un día promedio de ese día de la semana.
    average_sales: Decimal

    @classmethod
    def build(cls, cell: HourlyCell) -> HourlyCellResponse:
        return cls(
            weekday=cell.weekday,
            weekday_label=cell.weekday_label,
            hour=cell.hour,
            paid_orders=cell.paid_orders,
            sales=cell.sales,
            average_sales=cell.average_sales,
        )


class HourlySalesResponse(BaseModel):
    period: PeriodResponse
    # Siempre 168 celdas, de lunes a domingo y de 0 a 23 h.
    cells: list[HourlyCellResponse]
    # La celda con más ventas; `null` si no hubo ninguna.
    peak: HourlyCellResponse | None

    @classmethod
    def build(cls, report: Report[list[HourlyCell]]) -> HourlySalesResponse:
        cells = [HourlyCellResponse.build(cell) for cell in report.data]
        busiest = max(cells, key=lambda cell: cell.sales, default=None)
        return cls(
            period=_period(report),
            cells=cells,
            peak=busiest if busiest is not None and busiest.sales > 0 else None,
        )


class TopDishResponse(BaseModel):
    menu_item_id: int
    name: str
    quantity: int
    revenue: Decimal

    @classmethod
    def build(cls, dish: SoldDish) -> TopDishResponse:
        return cls(
            menu_item_id=dish.menu_item_id,
            name=dish.name,
            quantity=dish.quantity,
            revenue=dish.revenue,
        )


class TopDishesResponse(BaseModel):
    period: PeriodResponse
    dishes: list[TopDishResponse]

    @classmethod
    def build(cls, report: Report[list[SoldDish]]) -> TopDishesResponse:
        return cls(period=_period(report), dishes=[TopDishResponse.build(d) for d in report.data])


class DishMarginResponse(BaseModel):
    menu_item_id: int
    name: str
    category: str
    price: Decimal
    # `null` cuando el plato no tiene receta: el costo no se conoce.
    recipe_cost: Decimal | None
    unit_margin: Decimal | None
    margin_percent: Decimal | None
    quantity_sold: int
    revenue: Decimal
    estimated_cost: Decimal | None
    gross_margin: Decimal | None

    @classmethod
    def build(cls, row: DishMargin) -> DishMarginResponse:
        return cls(
            menu_item_id=row.menu_item_id,
            name=row.name,
            category=row.category,
            price=row.price,
            recipe_cost=row.recipe_cost,
            unit_margin=row.unit_margin,
            margin_percent=row.margin_percent,
            quantity_sold=row.quantity_sold,
            revenue=row.revenue,
            estimated_cost=row.estimated_cost,
            gross_margin=row.gross_margin,
        )


class DishMarginsResponse(BaseModel):
    period: PeriodResponse
    dishes: list[DishMarginResponse]

    @classmethod
    def build(cls, report: Report[list[DishMargin]]) -> DishMarginsResponse:
        return cls(
            period=_period(report), dishes=[DishMarginResponse.build(d) for d in report.data]
        )


class PaymentShareResponse(BaseModel):
    method: str
    label: str
    paid_orders: int
    amount: Decimal
    share_percent: Decimal

    @classmethod
    def build(cls, share: PaymentShare) -> PaymentShareResponse:
        return cls(
            method=share.method,
            label=share.label,
            paid_orders=share.paid_orders,
            amount=share.amount,
            share_percent=share.share_percent,
        )


class PaymentMixResponse(BaseModel):
    period: PeriodResponse
    total: Decimal
    methods: list[PaymentShareResponse]

    @classmethod
    def build(cls, report: Report[list[PaymentShare]]) -> PaymentMixResponse:
        return cls(
            period=_period(report),
            total=sum((share.amount for share in report.data), Decimal("0.00")),
            methods=[PaymentShareResponse.build(share) for share in report.data],
        )


class WaiterPerformanceResponse(BaseModel):
    waiter_id: int
    name: str
    paid_orders: int
    sales: Decimal
    average_ticket: Decimal
    cancelled_orders: int
    # Propinas de sus mesas en el rango; aparte de las ventas.
    tips: Decimal

    @classmethod
    def build(cls, row: WaiterPerformance) -> WaiterPerformanceResponse:
        return cls(
            waiter_id=row.waiter_id,
            name=row.name,
            paid_orders=row.paid_orders,
            sales=row.sales,
            average_ticket=row.average_ticket,
            cancelled_orders=row.cancelled_orders,
            tips=row.tips,
        )


class WaitersResponse(BaseModel):
    period: PeriodResponse
    waiters: list[WaiterPerformanceResponse]

    @classmethod
    def build(cls, report: Report[list[WaiterPerformance]]) -> WaitersResponse:
        return cls(
            period=_period(report),
            waiters=[WaiterPerformanceResponse.build(row) for row in report.data],
        )


# -- Almacén -----------------------------------------------------------------


class LowStockResponse(BaseModel):
    ingredient_id: int
    name: str
    unit: str
    stock: Decimal
    min_stock: Decimal
    # Lo que falta para llegar al mínimo.
    missing: Decimal

    @classmethod
    def build(cls, fact: StockFact) -> LowStockResponse:
        return cls(
            ingredient_id=fact.ingredient_id,
            name=fact.name,
            unit=fact.unit,
            stock=fact.stock,
            min_stock=fact.min_stock,
            missing=fact.min_stock - fact.stock,
        )


class WasteByIngredientResponse(BaseModel):
    ingredient_id: int
    name: str
    unit: str
    events: int
    quantity: Decimal
    cost: Decimal


class WasteByCauseResponse(BaseModel):
    # `null` agrupa las mermas que todavía no se clasificaron.
    cause: WasteCause | None
    label: str
    events: int
    cost: Decimal
    share_percent: Decimal


class WasteReportResponse(BaseModel):
    period: PeriodResponse
    events: int
    total_cost: Decimal
    # Mermas del rango sin causa: el panel puede ofrecer clasificarlas.
    pending_classification: int
    by_ingredient: list[WasteByIngredientResponse]
    by_cause: list[WasteByCauseResponse]

    @classmethod
    def build(cls, report: Report[WasteReport]) -> WasteReportResponse:
        data = report.data
        return cls(
            period=_period(report),
            events=data.events,
            total_cost=data.total_cost,
            pending_classification=data.pending_classification,
            by_ingredient=[
                WasteByIngredientResponse(
                    ingredient_id=row.ingredient_id,
                    name=row.name,
                    unit=row.unit,
                    events=row.events,
                    quantity=row.quantity,
                    cost=row.cost,
                )
                for row in data.by_ingredient
            ],
            by_cause=[
                WasteByCauseResponse(
                    cause=row.cause,
                    label=row.label,
                    events=row.events,
                    cost=row.cost,
                    share_percent=row.share_percent,
                )
                for row in data.by_cause
            ],
        )


# -- Decisiones --------------------------------------------------------------


def _fallback_label(reason: FallbackReason | None) -> str | None:
    return reason.label if reason is not None else None


class RestockItemResponse(BaseModel):
    ingredient_id: int
    name: str
    unit: str
    stock: Decimal
    min_stock: Decimal
    below_minimum: bool
    daily_use_7d: Decimal
    daily_use_28d: Decimal
    # `null` si el insumo no se usó: no se puede decir para cuánto alcanza.
    coverage_days: Decimal | None
    trend: str
    trend_label: str
    usage_change_percent: Decimal | None
    wasted_28d: Decimal
    waste_share_percent: Decimal
    days_since_last_purchase: int | None
    action: RestockAction
    action_label: str
    # 0 baja, 1 media, 2 alta, 3 crítica.
    urgency: int
    urgency_label: str
    # Con Jev puede caer entre dos niveles.
    urgency_score: float
    # De 0 a 1. `null` cuando decidieron las reglas.
    confidence: float | None
    engine: Engine
    engine_label: str
    model: str | None
    fallback_reason: FallbackReason | None
    fallback_label: str | None
    explanation: str
    # `null` si nunca se actualizó: es lo que dirían las reglas ahora.
    decision_id: int | None
    decided_at: datetime | None
    is_stale: bool

    @classmethod
    def build(cls, item: RestockItem) -> RestockItemResponse:
        facts, verdict = item.facts, item.verdict
        return cls(
            ingredient_id=facts.ingredient_id,
            name=facts.name,
            unit=facts.unit,
            stock=facts.stock,
            min_stock=facts.min_stock,
            below_minimum=facts.below_minimum,
            daily_use_7d=facts.daily_use_short,
            daily_use_28d=facts.daily_use_long,
            coverage_days=facts.coverage_days,
            trend=facts.trend.value,
            trend_label=facts.trend.label,
            usage_change_percent=facts.usage_change_percent,
            wasted_28d=facts.wasted_long,
            waste_share_percent=(facts.waste_share * 100).quantize(Decimal("0.1")),
            days_since_last_purchase=facts.days_since_last_purchase,
            action=verdict.outcome.action,
            action_label=verdict.outcome.action.label,
            urgency=int(verdict.outcome.urgency),
            urgency_label=verdict.outcome.urgency.label,
            urgency_score=verdict.outcome.urgency_score,
            confidence=verdict.confidence,
            engine=verdict.engine,
            engine_label=verdict.engine.label,
            model=verdict.model,
            fallback_reason=verdict.fallback,
            fallback_label=_fallback_label(verdict.fallback),
            explanation=item.explanation,
            decision_id=item.decision_id,
            decided_at=item.decided_at,
            is_stale=item.is_stale,
        )


class RestockResponse(BaseModel):
    # Cuándo se actualizó por última vez; `null` si nunca.
    refreshed_at: datetime | None
    # Cuántos insumos hay en cada acción, para los contadores del panel.
    counts: dict[RestockAction, int]
    items: list[RestockItemResponse]

    @classmethod
    def build(cls, items: list[RestockItem]) -> RestockResponse:
        moments = [item.decided_at for item in items if item.decided_at is not None]
        return cls(
            refreshed_at=max(moments, default=None),
            counts={
                action: sum(1 for item in items if item.verdict.outcome.action is action)
                for action in RestockAction
            },
            items=[RestockItemResponse.build(item) for item in items],
        )


class NoteClassificationResponse(BaseModel):
    order_id: int
    # `null` en la nota general del pedido.
    order_item_id: int | None
    scope: Literal["item", "order"]
    dish_name: str
    note: str
    status: Literal["classified", "pending"]
    # Lo que el tablero resalta. `null` mientras está pendiente.
    mentions_allergy: bool | None
    note_type: NoteType | None
    note_type_label: str | None
    allergy_probability: float | None
    confidence: float | None
    engine: Engine | None
    decided_at: datetime | None

    @classmethod
    def build(cls, view: NoteView) -> NoteClassificationResponse:
        note, outcome = view.note, view.outcome
        is_item = note.subject_type is SubjectType.ORDER_ITEM
        return cls(
            order_id=note.order_id,
            order_item_id=note.subject_id if is_item else None,
            scope="item" if is_item else "order",
            dish_name=note.dish_name,
            note=note.text,
            status="classified" if outcome is not None else "pending",
            mentions_allergy=outcome.mentions_allergy if outcome else None,
            note_type=outcome.note_type if outcome else None,
            note_type_label=outcome.note_type.label if outcome else None,
            allergy_probability=outcome.allergy_probability if outcome else None,
            confidence=view.confidence,
            engine=view.engine,
            decided_at=view.decided_at,
        )


class OrderNotesResponse(BaseModel):
    items: list[NoteClassificationResponse]


class ClassifyNotesResponse(BaseModel):
    classified: int
    already_classified: int
    allergies: int
    # Solo las recién clasificadas.
    items: list[NoteClassificationResponse]

    @classmethod
    def build(cls, run: ClassificationRun) -> ClassifyNotesResponse:
        return cls(
            classified=len(run.classified),
            already_classified=run.already_classified,
            allergies=sum(
                1 for view in run.classified if view.outcome and view.outcome.mentions_allergy
            ),
            items=[NoteClassificationResponse.build(view) for view in run.classified],
        )


class CauseCount(BaseModel):
    cause: WasteCause
    label: str
    count: int


class ClassifyWasteResponse(BaseModel):
    classified: int
    # Siguen sin clasificar porque pasaban del tope por pedido.
    remaining: int
    by_cause: list[CauseCount]

    @classmethod
    def build(cls, run: WasteClassificationRun) -> ClassifyWasteResponse:
        return cls(
            classified=run.classified,
            remaining=run.remaining,
            by_cause=[
                CauseCount(cause=cause, label=cause.label, count=count)
                for cause, count in sorted(run.by_cause.items(), key=lambda pair: -pair[1])
            ],
        )


class AiDecisionResponse(BaseModel):
    id: int
    kind: DecisionKind
    kind_label: str
    subject_type: SubjectType
    subject_id: int
    # El número del día del pedido, si el asunto es un pedido o uno de sus platos.
    order_number: int | None
    # El nombre del insumo o del plato; `None` si el asunto es el pedido entero.
    subject_label: str | None
    engine: Engine
    engine_label: str
    model: str | None
    # De 0 a 1 con Jev; `None` con reglas, que no tienen una (ver `confidence_kind`).
    confidence: float | None
    confidence_kind: ConfidenceKind
    confidence_kind_label: str
    fallback_reason: FallbackReason | None
    fallback_label: str | None
    # Lo que se le mostró al motor y lo que decidió, tal como se guardó.
    input_state: dict[str, Any]
    output: dict[str, Any]
    created_at: datetime

    @classmethod
    def build(cls, entry: DecisionEntry) -> AiDecisionResponse:
        decision = entry.decision
        raw_fallback = decision.output.get("fallback_reason")
        fallback = FallbackReason(raw_fallback) if raw_fallback else None
        certainty = confidence_kind(decision.engine)
        return cls(
            id=decision.id or 0,
            kind=decision.kind,
            kind_label=decision.kind.label,
            subject_type=decision.subject_type,
            subject_id=decision.subject_id,
            order_number=entry.subject.order_number,
            subject_label=entry.subject.label,
            engine=decision.engine,
            engine_label=decision.engine.label,
            model=decision.model,
            confidence=decision.confidence,
            confidence_kind=certainty,
            confidence_kind_label=certainty.label,
            fallback_reason=fallback,
            fallback_label=_fallback_label(fallback),
            input_state=decision.input_state,
            output=decision.output,
            created_at=decision.created_at,
        )


class AiDecisionPageResponse(BaseModel):
    items: list[AiDecisionResponse]
    total: int

    @classmethod
    def build(cls, page: Page[DecisionEntry]) -> AiDecisionPageResponse:
        return cls(items=[AiDecisionResponse.build(e) for e in page.items], total=page.total)
