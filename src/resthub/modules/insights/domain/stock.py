"""Indicadores del almacén: stock bajo, mermas y los números de la reposición.

Python puro. Los números se calculan acá, en código, y no los estima la IA:
cuánto queda, cuánto se usa por día, para cuántos días alcanza y si el uso
sube o baja. A Jev se le pregunta solo qué hacer con esos números.

Las cantidades van en la unidad del insumo (g, ml o unidades), como en el
inventario.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from resthub.modules.insights.domain.decisions import WasteCause
from resthub.modules.insights.domain.sales import money, percent

QUANTITY_STEP = Decimal("0.001")
ZERO = Decimal("0")
# Las dos ventanas de consumo: la semana dice cómo viene el uso ahora, las
# cuatro semanas dan la base contra la que se compara.
SHORT_WINDOW_DAYS = 7
LONG_WINDOW_DAYS = 28
# Cuánto tiene que separarse la semana del promedio de cuatro semanas para
# hablar de tendencia y no de ruido.
TREND_THRESHOLD = Decimal("0.20")


def quantity(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_STEP, ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class StockFact:
    """Un insumo activo con su stock, que es la suma del libro."""

    ingredient_id: int
    name: str
    unit: str
    stock: Decimal
    min_stock: Decimal

    @property
    def is_low(self) -> bool:
        # Igual que en el inventario: con el mínimo justo todavía alcanza.
        return self.stock < self.min_stock


def low_stock(facts: Iterable[StockFact]) -> list[StockFact]:
    """Los que están bajo el mínimo, del más comprometido al menos."""

    def pressure(fact: StockFact) -> Decimal:
        if fact.min_stock <= 0:
            return ZERO
        return fact.stock / fact.min_stock

    return sorted((fact for fact in facts if fact.is_low), key=lambda f: (pressure(f), f.name))


# -- Reposición --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngredientFlow:
    """Lo que salió de un insumo en las dos ventanas, en positivo."""

    consumed_short: Decimal = ZERO
    consumed_long: Decimal = ZERO
    wasted_long: Decimal = ZERO
    # El primer movimiento del insumo: un insumo nuevo no puede promediar
    # sobre cuatro semanas que no existió.
    first_movement_at: datetime | None = None
    last_purchase_at: datetime | None = None


class UsageTrend(StrEnum):
    RISING = "rising"
    STABLE = "stable"
    FALLING = "falling"
    NO_DATA = "no_data"

    @property
    def label(self) -> str:
        return _TREND_LABELS[self]


_TREND_LABELS: dict[UsageTrend, str] = {
    UsageTrend.RISING: "En alza",
    UsageTrend.STABLE: "Estable",
    UsageTrend.FALLING: "En baja",
    UsageTrend.NO_DATA: "Sin datos",
}


@dataclass(frozen=True, slots=True)
class RestockFacts:
    """Todo lo que se sabe de un insumo para decidir si comprarlo."""

    ingredient_id: int
    name: str
    unit: str
    stock: Decimal
    min_stock: Decimal
    daily_use_short: Decimal
    daily_use_long: Decimal
    wasted_long: Decimal
    consumed_long: Decimal
    days_since_last_purchase: int | None

    @property
    def below_minimum(self) -> bool:
        return self.stock < self.min_stock

    @property
    def reference_daily_use(self) -> Decimal:
        """El uso diario contra el que se mide la cobertura: el más reciente que haya."""
        return self.daily_use_short if self.daily_use_short > 0 else self.daily_use_long

    @property
    def coverage_days(self) -> Decimal | None:
        """Para cuántos días alcanza lo que hay. `None` si no se usa."""
        if self.stock <= 0:
            return Decimal("0.0")
        use = self.reference_daily_use
        if use <= 0:
            return None
        return (self.stock / use).quantize(Decimal("0.1"), ROUND_HALF_UP)

    @property
    def usage_change_percent(self) -> Decimal | None:
        if self.daily_use_long <= 0:
            return None
        return percent(self.daily_use_short - self.daily_use_long, self.daily_use_long)

    @property
    def trend(self) -> UsageTrend:
        if self.daily_use_long <= 0:
            return UsageTrend.NO_DATA if self.daily_use_short <= 0 else UsageTrend.RISING
        change = (self.daily_use_short - self.daily_use_long) / self.daily_use_long
        if change > TREND_THRESHOLD:
            return UsageTrend.RISING
        if change < -TREND_THRESHOLD:
            return UsageTrend.FALLING
        return UsageTrend.STABLE

    @property
    def waste_share(self) -> Decimal:
        """Qué parte de lo que salió del almacén en cuatro semanas fue a la basura."""
        outflow = self.consumed_long + self.wasted_long
        if outflow <= 0:
            return ZERO
        return (self.wasted_long / outflow).quantize(Decimal("0.001"), ROUND_HALF_UP)


def restock_facts(stock: StockFact, flow: IngredientFlow, now: datetime) -> RestockFacts:
    """Arma los números de un insumo a partir de su stock y de lo que salió.

    Un insumo con menos historia que la ventana promedia sobre los días que
    lleva registrado, con un mínimo de uno: si se creó hace tres días, su
    consumo de "cuatro semanas" se divide entre tres y no entre veintiocho.
    """
    lived = LONG_WINDOW_DAYS
    if flow.first_movement_at is not None:
        lived = max(1, (now - flow.first_movement_at).days)
    short_days = min(SHORT_WINDOW_DAYS, lived)
    long_days = min(LONG_WINDOW_DAYS, lived)
    since_purchase = (
        (now - flow.last_purchase_at).days if flow.last_purchase_at is not None else None
    )
    return RestockFacts(
        ingredient_id=stock.ingredient_id,
        name=stock.name,
        unit=stock.unit,
        stock=quantity(stock.stock),
        min_stock=quantity(stock.min_stock),
        daily_use_short=quantity(flow.consumed_short / short_days),
        daily_use_long=quantity(flow.consumed_long / long_days),
        wasted_long=quantity(flow.wasted_long),
        consumed_long=quantity(flow.consumed_long),
        days_since_last_purchase=since_purchase,
    )


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def restock_state(facts: RestockFacts) -> dict[str, Any]:
    """Lo que se le muestra a Jev, y lo que se guarda como entrada de la decisión.

    Claves en inglés, el idioma en que Jev es más preciso; el nombre del insumo
    va tal cual. Los números van ya calculados: el modelo decide, no suma.
    """
    return {
        "context": (
            "Small family restaurant in Peru. Ingredients are bought at the local market, "
            "so a purchase made today is available for the next service."
        ),
        "ingredient": {"name": facts.name, "unit": facts.unit},
        "stock_on_hand": float(facts.stock),
        "minimum_stock": float(facts.min_stock),
        "below_minimum": facts.below_minimum,
        "average_daily_use_last_7_days": float(facts.daily_use_short),
        "average_daily_use_last_28_days": float(facts.daily_use_long),
        "days_of_cover": _number(facts.coverage_days),
        "usage_trend": facts.trend.value,
        "usage_change_percent": _number(facts.usage_change_percent),
        "wasted_last_28_days": float(facts.wasted_long),
        "waste_share_of_outflow_percent": float(facts.waste_share * 100),
        "days_since_last_purchase": facts.days_since_last_purchase,
    }


# -- Mermas ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WasteFact:
    """Una merma del libro, con su causa si ya se clasificó."""

    movement_id: int
    ingredient_id: int
    ingredient_name: str
    unit: str
    # Lo que se perdió, en positivo.
    quantity: Decimal
    # Lo que valía, al costo vigente cuando se registró.
    cost: Decimal
    reason: str
    created_at: datetime
    cause: WasteCause | None = None


def waste_state(waste: WasteFact) -> dict[str, Any]:
    return {
        "reason": waste.reason,
        "reason_language": "Spanish (Peru)",
        "ingredient": waste.ingredient_name,
        "quantity": f"{quantity(waste.quantity).normalize():f} {waste.unit}",
    }


@dataclass(frozen=True, slots=True)
class WasteByIngredient:
    ingredient_id: int
    name: str
    unit: str
    events: int
    quantity: Decimal
    cost: Decimal


@dataclass(frozen=True, slots=True)
class WasteByCause:
    # `None` agrupa lo que todavía no se clasificó.
    cause: WasteCause | None
    events: int
    cost: Decimal
    share_percent: Decimal

    @property
    def label(self) -> str:
        return self.cause.label if self.cause is not None else "Sin clasificar"


@dataclass(frozen=True, slots=True)
class WasteReport:
    events: int
    total_cost: Decimal
    pending_classification: int
    by_ingredient: list[WasteByIngredient]
    by_cause: list[WasteByCause]


def waste_report(wastes: Iterable[WasteFact]) -> WasteReport:
    items = list(wastes)
    per_ingredient: dict[int, list[WasteFact]] = defaultdict(list)
    per_cause: dict[WasteCause | None, list[WasteFact]] = defaultdict(list)
    for waste in items:
        per_ingredient[waste.ingredient_id].append(waste)
        per_cause[waste.cause].append(waste)
    total_cost = sum((waste.cost for waste in items), ZERO)

    by_ingredient = [
        WasteByIngredient(
            ingredient_id=ingredient_id,
            name=group[0].ingredient_name,
            unit=group[0].unit,
            events=len(group),
            quantity=quantity(sum((waste.quantity for waste in group), ZERO)),
            cost=money(sum((waste.cost for waste in group), ZERO)),
        )
        for ingredient_id, group in per_ingredient.items()
    ]
    by_cause = [
        WasteByCause(
            cause=cause,
            events=len(group),
            cost=money(sum((waste.cost for waste in group), ZERO)),
            share_percent=percent(sum((waste.cost for waste in group), ZERO), total_cost),
        )
        for cause, group in per_cause.items()
    ]
    return WasteReport(
        events=len(items),
        total_cost=money(total_cost),
        pending_classification=len(per_cause.get(None, [])),
        by_ingredient=sorted(by_ingredient, key=lambda row: (-row.cost, row.name)),
        by_cause=sorted(by_cause, key=lambda row: (row.cause is None, -row.cost)),
    )
