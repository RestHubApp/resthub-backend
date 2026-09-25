"""Reportes del almacén: insumos bajo el mínimo y mermas."""

from __future__ import annotations

from dataclasses import replace

from resthub.modules.insights.domain.decisions import DecisionKind, SubjectType, WasteCause
from resthub.modules.insights.domain.stock import StockFact, WasteReport, low_stock, waste_report
from resthub.modules.insights.ports.decision_log import DecisionLog
from resthub.modules.insights.ports.restaurant_calendar import RestaurantCalendar
from resthub.modules.insights.ports.stock_directory import StockDirectory
from resthub.modules.insights.use_cases.shared import PeriodQuery, Report, resolve_period


class ReadLowStock:
    def __init__(self, stock: StockDirectory) -> None:
        self._stock = stock

    async def __call__(self, restaurant_id: int) -> list[StockFact]:
        return low_stock(await self._stock.stock_levels(restaurant_id))


class ReadWasteReport:
    """Mermas del rango por insumo y por causa.

    La causa es la última clasificación guardada de cada merma; la que todavía
    no se clasificó cuenta aparte, para que el panel ofrezca clasificarla.
    """

    def __init__(
        self, calendar: RestaurantCalendar, stock: StockDirectory, decisions: DecisionLog
    ) -> None:
        self._calendar = calendar
        self._stock = stock
        self._decisions = decisions

    async def __call__(self, query: PeriodQuery) -> Report[WasteReport]:
        period, timezone = await resolve_period(self._calendar, query)
        since, until = period.window(timezone)
        wastes = await self._stock.wastes(query.restaurant_id, since=since, until=until)
        causes = await self._decisions.latest(
            query.restaurant_id,
            DecisionKind.WASTE_CAUSE,
            SubjectType.STOCK_MOVEMENT,
            {waste.movement_id for waste in wastes},
        )
        classified = [
            replace(waste, cause=WasteCause(causes[waste.movement_id].output["cause"]))
            if waste.movement_id in causes
            else waste
            for waste in wastes
        ]
        return Report(period=period, timezone=timezone, data=waste_report(classified))
