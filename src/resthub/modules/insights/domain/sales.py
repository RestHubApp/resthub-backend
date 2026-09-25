"""Indicadores de venta: cuentas deterministas sobre pedidos ya cerrados.

Python puro. Los números salen de los pedidos pagados y cancelados y de sus
platos; acá no hay IA ni estimaciones. Todo se agrega en código y no en SQL
para que las cuentas sean las mismas en SQLite y en PostgreSQL, y para poder
probarlas con datos armados a mano.

Una venta es un pedido pagado, contado en el día del restaurante en que se
abrió (`business_date`). El total del pedido ya viene calculado por `orders`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from resthub.modules.insights.domain.period import DateRange

CENT = Decimal("0.01")
TENTH = Decimal("0.1")
ZERO = Decimal("0.00")

PAID = "paid"
CANCELLED = "cancelled"

# Los mismos medios que acepta `orders`, con su nombre en pantalla. Se repiten
# acá porque este módulo no puede importar aquel.
PAYMENT_METHOD_LABELS: dict[str, str] = {
    "cash": "Efectivo",
    "yape": "Yape",
    "plin": "Plin",
    "card": "Tarjeta",
    "transfer": "Transferencia",
}

WEEKDAY_LABELS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


def percent(part: Decimal, whole: Decimal) -> Decimal:
    if whole == 0:
        return Decimal("0.0")
    return (part * 100 / whole).quantize(TENTH, ROUND_HALF_UP)


def change_percent(current: Decimal, previous: Decimal) -> Decimal | None:
    """Cuánto cambió respecto del período anterior; `None` si antes no hubo nada."""
    if previous == 0:
        return None
    return percent(current - previous, previous)


def average(total: Decimal, count: int) -> Decimal:
    return money(total / count) if count else ZERO


@dataclass(frozen=True, slots=True)
class OrderFact:
    """Lo que un reporte necesita de un pedido cerrado."""

    id: int
    business_date: date
    # Cuándo se abrió, con zona: define la hora del día en el mapa de calor.
    created_at: datetime
    status: str
    total: Decimal
    payment_method: str | None
    waiter_id: int

    @property
    def is_paid(self) -> bool:
        return self.status == PAID

    @property
    def is_cancelled(self) -> bool:
        return self.status == CANCELLED


def _paid(orders: Iterable[OrderFact]) -> list[OrderFact]:
    return [order for order in orders if order.is_paid]


# -- Resumen -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SalesSummary:
    sales: Decimal
    paid_orders: int
    average_ticket: Decimal
    cancelled_orders: int
    # Lo que sumaban los pedidos cancelados: plata que no entró.
    cancelled_amount: Decimal


def summarize(orders: Iterable[OrderFact]) -> SalesSummary:
    closed = list(orders)
    paid = _paid(closed)
    cancelled = [order for order in closed if order.is_cancelled]
    sales = sum((order.total for order in paid), ZERO)
    return SalesSummary(
        sales=money(sales),
        paid_orders=len(paid),
        average_ticket=average(sales, len(paid)),
        cancelled_orders=len(cancelled),
        cancelled_amount=money(sum((order.total for order in cancelled), ZERO)),
    )


# -- Por día -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailySales:
    day: date
    sales: Decimal
    paid_orders: int
    average_ticket: Decimal


def daily_sales(orders: Iterable[OrderFact], period: DateRange) -> list[DailySales]:
    """Un punto por cada día del rango, también los días sin ventas.

    Un día sin ventas aparece en cero y no se omite: si faltara, el gráfico
    uniría el día anterior con el siguiente y escondería el hueco.
    """
    sales: dict[date, Decimal] = defaultdict(lambda: ZERO)
    counts: dict[date, int] = defaultdict(int)
    for order in _paid(orders):
        sales[order.business_date] += order.total
        counts[order.business_date] += 1
    return [
        DailySales(
            day=day,
            sales=money(sales[day]),
            paid_orders=counts[day],
            average_ticket=average(sales[day], counts[day]),
        )
        for day in period.each_day()
    ]


# -- Por hora ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HourlyCell:
    # 0 es lunes, como en `date.weekday()`.
    weekday: int
    hour: int
    paid_orders: int
    sales: Decimal
    # Venta promedio de esa hora en un día de esa semana: el total se divide
    # entre cuántos lunes (o martes...) tiene el rango. Sin eso, un rango con
    # cinco sábados y cuatro lunes haría ver más lleno el sábado de lo que es.
    average_sales: Decimal

    @property
    def weekday_label(self) -> str:
        return WEEKDAY_LABELS[self.weekday]


def hourly_heatmap(
    orders: Iterable[OrderFact], period: DateRange, timezone: str
) -> list[HourlyCell]:
    """Las 168 celdas de la semana (7 días por 24 horas), en la hora del local.

    La hora es la de apertura del pedido, que es cuando llega la gente; el
    cobro puede ser una hora después.
    """
    zone = ZoneInfo(timezone)
    sales: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for order in _paid(orders):
        local = order.created_at.astimezone(zone)
        key = (local.weekday(), local.hour)
        sales[key] += order.total
        counts[key] += 1
    occurrences: dict[int, int] = defaultdict(int)
    for day in period.each_day():
        occurrences[day.weekday()] += 1
    return [
        HourlyCell(
            weekday=weekday,
            hour=hour,
            paid_orders=counts[(weekday, hour)],
            sales=money(sales[(weekday, hour)]),
            average_sales=average(sales[(weekday, hour)], occurrences[weekday]),
        )
        for weekday in range(7)
        for hour in range(24)
    ]


# -- Platos ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SoldDish:
    """Un plato con lo vendido en pedidos pagados del rango."""

    menu_item_id: int
    name: str
    quantity: int
    revenue: Decimal


def top_dishes(sold: Iterable[SoldDish], limit: int) -> list[SoldDish]:
    """Los más vendidos por cantidad; a igual cantidad, el que más facturó."""
    ranked = sorted(sold, key=lambda dish: (-dish.quantity, -dish.revenue, dish.name))
    return [
        SoldDish(
            menu_item_id=dish.menu_item_id,
            name=dish.name,
            quantity=dish.quantity,
            revenue=money(dish.revenue),
        )
        for dish in ranked[:limit]
    ]


@dataclass(frozen=True, slots=True)
class CatalogDish:
    """Un plato de la carta con el costo de una porción según su receta."""

    menu_item_id: int
    name: str
    category: str
    price: Decimal
    is_active: bool
    # `None` si no tiene receta: no es que cueste cero, es que no se sabe.
    recipe_cost: Decimal | None


@dataclass(frozen=True, slots=True)
class DishMargin:
    menu_item_id: int
    name: str
    category: str
    price: Decimal
    recipe_cost: Decimal | None
    # Precio menos costo de una porción.
    unit_margin: Decimal | None
    margin_percent: Decimal | None
    quantity_sold: int
    revenue: Decimal
    # Lo que costaron, a costo de receta de hoy, las porciones vendidas.
    estimated_cost: Decimal | None
    # Lo que dejaron: ingresos menos ese costo.
    gross_margin: Decimal | None


def dish_margins(catalog: Iterable[CatalogDish], sold: Iterable[SoldDish]) -> list[DishMargin]:
    """Margen de cada plato de la carta y de lo que dejó en el rango.

    Entran los platos activos y también los retirados que se vendieron en el
    rango. El costo es el de la receta con el costo vigente de cada insumo:
    responde "cuánto me deja este plato hoy", no cuánto costó cocinarlo el mes
    pasado. El margen unitario se calcula sobre el costo ya redondeado, como en
    el inventario, para que costo más margen den el precio exacto.
    """
    sold_by_id = {dish.menu_item_id: dish for dish in sold}
    rows: list[DishMargin] = []
    for dish in catalog:
        sales = sold_by_id.get(dish.menu_item_id)
        if not dish.is_active and sales is None:
            continue
        quantity = sales.quantity if sales else 0
        revenue = money(sales.revenue) if sales else ZERO
        cost = money(dish.recipe_cost) if dish.recipe_cost is not None else None
        estimated = money(dish.recipe_cost * quantity) if dish.recipe_cost is not None else None
        rows.append(
            DishMargin(
                menu_item_id=dish.menu_item_id,
                name=dish.name,
                category=dish.category,
                price=money(dish.price),
                recipe_cost=cost,
                unit_margin=money(dish.price - cost) if cost is not None else None,
                margin_percent=(
                    percent(dish.price - dish.recipe_cost, dish.price)
                    if dish.recipe_cost is not None and dish.price > 0
                    else None
                ),
                quantity_sold=quantity,
                revenue=revenue,
                estimated_cost=estimated,
                gross_margin=revenue - estimated if estimated is not None else None,
            )
        )
    # Primero lo que más margen dejó en el rango; los sin receta, al final.
    return sorted(
        rows,
        key=lambda row: (
            row.gross_margin is None,
            -(row.gross_margin or ZERO),
            -row.quantity_sold,
            row.name,
        ),
    )


# -- Medios de pago ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaymentShare:
    method: str
    paid_orders: int
    amount: Decimal
    share_percent: Decimal

    @property
    def label(self) -> str:
        return PAYMENT_METHOD_LABELS.get(self.method, self.method)


def payment_mix(orders: Iterable[OrderFact]) -> list[PaymentShare]:
    """Cuánto entró por cada medio. Un medio sin cobros en el rango no aparece."""
    amounts: dict[str, Decimal] = defaultdict(lambda: ZERO)
    counts: dict[str, int] = defaultdict(int)
    for order in _paid(orders):
        method = order.payment_method or "cash"
        amounts[method] += order.total
        counts[method] += 1
    total = sum(amounts.values(), ZERO)
    shares = [
        PaymentShare(
            method=method,
            paid_orders=counts[method],
            amount=money(amount),
            share_percent=percent(amount, total),
        )
        for method, amount in amounts.items()
    ]
    return sorted(shares, key=lambda share: (-share.amount, share.method))


# -- Personal ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WaiterPerformance:
    waiter_id: int
    name: str
    paid_orders: int
    sales: Decimal
    average_ticket: Decimal
    cancelled_orders: int


def waiter_performance(
    orders: Iterable[OrderFact], names: Mapping[int, str]
) -> list[WaiterPerformance]:
    """Pedidos cobrados y ventas de cada cuenta que abrió pedidos en el rango."""
    sales: dict[int, Decimal] = defaultdict(lambda: ZERO)
    paid: dict[int, int] = defaultdict(int)
    cancelled: dict[int, int] = defaultdict(int)
    for order in orders:
        if order.is_paid:
            sales[order.waiter_id] += order.total
            paid[order.waiter_id] += 1
        elif order.is_cancelled:
            cancelled[order.waiter_id] += 1
    waiters = set(paid) | set(cancelled)
    rows = [
        WaiterPerformance(
            waiter_id=waiter_id,
            name=names.get(waiter_id, ""),
            paid_orders=paid[waiter_id],
            sales=money(sales[waiter_id]),
            average_ticket=average(sales[waiter_id], paid[waiter_id]),
            cancelled_orders=cancelled[waiter_id],
        )
        for waiter_id in waiters
    ]
    return sorted(rows, key=lambda row: (-row.sales, row.name))
