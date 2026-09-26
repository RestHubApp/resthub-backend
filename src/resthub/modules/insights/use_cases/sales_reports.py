"""Reportes de venta del panel del encargado.

Todos exigen `insights.read` y miran solo el restaurante de quien pregunta.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.modules.insights.domain.period import DateRange
from resthub.modules.insights.domain.sales import (
    DailySales,
    DishMargin,
    HourlyCell,
    OrderFact,
    PaymentShare,
    SalesSummary,
    SoldDish,
    WaiterPerformance,
    daily_sales,
    dish_margins,
    hourly_heatmap,
    payment_mix,
    summarize,
    top_dishes,
    waiter_performance,
)
from resthub.modules.insights.ports.restaurant_calendar import RestaurantCalendar
from resthub.modules.insights.ports.sales_directory import SalesDirectory
from resthub.modules.insights.use_cases.shared import PeriodQuery, Report, resolve_period

DEFAULT_TOP_DISHES = 10


class _SalesReport:
    def __init__(self, calendar: RestaurantCalendar, sales: SalesDirectory) -> None:
        self._calendar = calendar
        self._sales = sales

    async def _orders(self, query: PeriodQuery) -> tuple[DateRange, str, list[OrderFact]]:
        period, timezone = await resolve_period(self._calendar, query)
        return period, timezone, await self._sales.closed_orders(query.restaurant_id, period)


@dataclass(frozen=True, slots=True)
class SummaryReport:
    current: SalesSummary
    # El rango del mismo largo justo anterior, para mostrar cuánto se subió o
    # se bajó sin una segunda petición.
    previous_period: DateRange
    previous: SalesSummary


class ReadSalesSummary(_SalesReport):
    async def __call__(self, query: PeriodQuery) -> Report[SummaryReport]:
        period, timezone = await resolve_period(self._calendar, query)
        previous = period.previous()
        # Una sola consulta cubre los dos rangos, que son contiguos.
        orders = await self._sales.closed_orders(
            query.restaurant_id, DateRange(start=previous.start, end=period.end)
        )
        current = [order for order in orders if order.business_date >= period.start]
        before = [order for order in orders if order.business_date < period.start]
        return Report(
            period=period,
            timezone=timezone,
            data=SummaryReport(
                current=summarize(current), previous_period=previous, previous=summarize(before)
            ),
        )


class ReadDailySales(_SalesReport):
    async def __call__(self, query: PeriodQuery) -> Report[list[DailySales]]:
        period, timezone, orders = await self._orders(query)
        return Report(period=period, timezone=timezone, data=daily_sales(orders, period))


class ReadHourlySales(_SalesReport):
    async def __call__(self, query: PeriodQuery) -> Report[list[HourlyCell]]:
        period, timezone, orders = await self._orders(query)
        return Report(
            period=period, timezone=timezone, data=hourly_heatmap(orders, period, timezone)
        )


class ReadPaymentMix(_SalesReport):
    async def __call__(self, query: PeriodQuery) -> Report[list[PaymentShare]]:
        period, timezone, _ = await self._orders(query)
        payments = await self._sales.payments(query.restaurant_id, period)
        return Report(period=period, timezone=timezone, data=payment_mix(payments))


class ReadWaiterPerformance(_SalesReport):
    async def __call__(self, query: PeriodQuery) -> Report[list[WaiterPerformance]]:
        period, timezone, orders = await self._orders(query)
        names = await self._sales.staff_names(
            query.restaurant_id, {order.waiter_id for order in orders}
        )
        payments = await self._sales.payments(query.restaurant_id, period)
        return Report(
            period=period, timezone=timezone, data=waiter_performance(orders, names, payments)
        )


class ReadTopDishes(_SalesReport):
    async def __call__(
        self, query: PeriodQuery, limit: int = DEFAULT_TOP_DISHES
    ) -> Report[list[SoldDish]]:
        period, timezone = await resolve_period(self._calendar, query)
        sold = await self._sales.sold_dishes(query.restaurant_id, period)
        return Report(period=period, timezone=timezone, data=top_dishes(sold, limit))


class ReadDishMargins(_SalesReport):
    async def __call__(self, query: PeriodQuery) -> Report[list[DishMargin]]:
        period, timezone = await resolve_period(self._calendar, query)
        sold = await self._sales.sold_dishes(query.restaurant_id, period)
        catalog = await self._sales.dish_catalog(query.restaurant_id)
        return Report(period=period, timezone=timezone, data=dish_margins(catalog, sold))
