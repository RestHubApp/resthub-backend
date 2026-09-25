"""Insumos, movimientos de stock y recetas.

Python puro. Sin FastAPI, sin SQLAlchemy, sin Pydantic.

El stock no se guarda en ningún lado: es la suma de los movimientos. Un número
de stock que se edita a mano se equivoca y no deja rastro de por qué; un libro
de movimientos se equivoca igual, pero cada error tiene fecha, autor y motivo, y
se corrige con otro movimiento (un ajuste), nunca borrando el anterior.

Las cantidades van en la unidad del insumo (gramos, mililitros o unidades) con
tres decimales. El costo unitario va por esa misma unidad, con seis decimales:
el arroz a S/ 4.20 el kilo cuesta S/ 0.0042 el gramo, y con menos decimales el
costo de las recetas se correría varios puntos.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from resthub.modules.inventory.domain.exceptions import (
    InvalidIngredient,
    InvalidMovement,
    InvalidRecipe,
)

MAX_NAME_LENGTH = 80
MAX_REASON_LENGTH = 200
QUANTITY_STEP = Decimal("0.001")
COST_STEP = Decimal("0.000001")
# Lo que entra en las columnas `Numeric(12, 3)` y `Numeric(14, 6)`.
MAX_QUANTITY = Decimal("999999999.999")
MAX_UNIT_COST = Decimal("99999999.999999")
ZERO = Decimal("0")
# El stock de un insumo sin movimientos, con la escala del libro.
NO_STOCK = ZERO.quantize(QUANTITY_STEP)


class Unit(StrEnum):
    GRAM = "g"
    MILLILITER = "ml"
    UNIT = "unit"

    @property
    def label(self) -> str:
        return _UNIT_LABELS[self]


_UNIT_LABELS: dict[Unit, str] = {
    Unit.GRAM: "gramos",
    Unit.MILLILITER: "mililitros",
    Unit.UNIT: "unidades",
}


class MovementKind(StrEnum):
    PURCHASE = "purchase"
    CONSUMPTION = "consumption"
    WASTE = "waste"
    ADJUSTMENT = "adjustment"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS: dict[MovementKind, str] = {
    MovementKind.PURCHASE: "Compra",
    MovementKind.CONSUMPTION: "Consumo",
    MovementKind.WASTE: "Merma",
    MovementKind.ADJUSTMENT: "Ajuste",
}


@dataclass(slots=True)
class Ingredient:
    restaurant_id: int
    name: str
    unit: Unit
    # Por debajo de esto, el insumo aparece en las alertas.
    min_stock: Decimal = ZERO
    # Costo por unidad del insumo (por gramo, por mililitro o por unidad).
    unit_cost: Decimal = ZERO
    is_active: bool = True
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.name = validate_name(self.name)
        self.min_stock = validate_min_stock(self.min_stock)
        self.unit_cost = validate_unit_cost(self.unit_cost)

    def rename(self, name: str) -> None:
        self.name = validate_name(name)

    def set_min_stock(self, min_stock: Decimal) -> None:
        self.min_stock = validate_min_stock(min_stock)

    def set_unit_cost(self, unit_cost: Decimal) -> None:
        self.unit_cost = validate_unit_cost(unit_cost)

    def absorb_purchase(self, stock_before: Decimal, quantity: Decimal, cost: Decimal) -> None:
        """Actualiza el costo con una compra, por promedio ponderado.

        Se elige el promedio ponderado y no el último costo porque el
        inventario mezcla lo viejo con lo nuevo: si quedaban 5 kg de arroz a
        S/ 4 y llegan 20 kg a S/ 5, lo que se cocina esta semana cuesta
        S/ 4.80 el kilo, no S/ 5. El último costo exageraría cada subida y
        cada bajada en el margen de los platos.

        Si el stock previo es cero o negativo no hay nada que ponderar: lo que
        se tiene es solo lo que acaba de llegar, a su precio.
        """
        if stock_before <= 0:
            self.unit_cost = validate_unit_cost(cost)
            return
        total = stock_before * self.unit_cost + quantity * cost
        self.unit_cost = validate_unit_cost(total / (stock_before + quantity))


@dataclass(slots=True)
class StockMovement:
    """Una línea del libro de inventario. Nunca se edita ni se borra."""

    restaurant_id: int
    ingredient_id: int
    kind: MovementKind
    # Con signo: positivo entra, negativo sale.
    quantity: Decimal
    created_by: int
    # En compras, lo que costó cada unidad. En consumos, el costo vigente al
    # descontar: así el costo de lo vendido un día no cambia si mañana sube.
    unit_cost: Decimal | None = None
    reason: str = ""
    order_id: int | None = None
    order_item_id: int | None = None
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.quantity = _quantize_quantity(self.quantity, InvalidMovement)
        self.reason = " ".join(self.reason.split())
        if len(self.reason) > MAX_REASON_LENGTH:
            raise InvalidMovement(f"El motivo admite {MAX_REASON_LENGTH} caracteres.")
        if self.unit_cost is not None:
            self.unit_cost = validate_unit_cost(self.unit_cost, InvalidMovement)
        _RULES[self.kind](self)


def _check_purchase(movement: StockMovement) -> None:
    if movement.quantity <= 0:
        raise InvalidMovement("Una compra tiene que sumar una cantidad positiva.")
    if movement.unit_cost is None:
        raise InvalidMovement("Una compra necesita su costo unitario.")


def _check_consumption(movement: StockMovement) -> None:
    if movement.quantity >= 0:
        raise InvalidMovement("Un consumo tiene que restar.")
    if movement.order_id is None or movement.order_item_id is None:
        raise InvalidMovement("Un consumo sale siempre de un plato servido.")


def _check_waste(movement: StockMovement) -> None:
    if movement.quantity >= 0:
        raise InvalidMovement("Una merma tiene que restar.")
    if not movement.reason:
        raise InvalidMovement("Una merma exige indicar el motivo.")


def _check_adjustment(movement: StockMovement) -> None:
    if movement.quantity == 0:
        raise InvalidMovement("Un ajuste de cero no cambia nada.")
    if not movement.reason:
        raise InvalidMovement("Un ajuste exige indicar el motivo.")


_RULES = {
    MovementKind.PURCHASE: _check_purchase,
    MovementKind.CONSUMPTION: _check_consumption,
    MovementKind.WASTE: _check_waste,
    MovementKind.ADJUSTMENT: _check_adjustment,
}


@dataclass(frozen=True, slots=True)
class StockLevel:
    """Un insumo con su stock calculado."""

    ingredient: Ingredient
    stock: Decimal

    @property
    def is_low(self) -> bool:
        # Estrictamente por debajo: con el mínimo justo todavía alcanza.
        return self.stock < self.ingredient.min_stock

    @property
    def is_negative(self) -> bool:
        """Se consumió más de lo que se registró como comprado.

        No se impide: el plato ya salió de la cocina y negarse a registrarlo
        solo escondería el problema. Se reporta, y casi siempre significa una
        compra sin anotar o una receta mal cargada.
        """
        return self.stock < 0


@dataclass(frozen=True, slots=True)
class RecipeLine:
    """Cuánto de un insumo lleva una porción de un plato."""

    ingredient_id: int
    quantity: Decimal

    def __post_init__(self) -> None:
        quantity = _quantize_quantity(self.quantity, InvalidRecipe)
        if quantity <= 0:
            raise InvalidRecipe("Cada insumo de la receta necesita una cantidad positiva.")
        object.__setattr__(self, "quantity", quantity)


def validate_recipe(lines: Iterable[RecipeLine]) -> list[RecipeLine]:
    recipe = list(lines)
    ids = [line.ingredient_id for line in recipe]
    if len(ids) != len(set(ids)):
        raise InvalidRecipe("Un insumo aparece dos veces en la receta; suma las cantidades.")
    return recipe


def recipe_cost(lines: Iterable[RecipeLine], ingredients: Mapping[int, Ingredient]) -> Decimal:
    """Costo de una porción, sin redondear: redondear es cosa de quien lo muestra."""
    return sum(
        (
            line.quantity * ingredients[line.ingredient_id].unit_cost
            for line in lines
            if line.ingredient_id in ingredients
        ),
        ZERO,
    )


def validate_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise InvalidIngredient("El nombre del insumo no puede quedar vacío.")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidIngredient(f"El nombre del insumo no puede pasar de {MAX_NAME_LENGTH}.")
    return name


def validate_min_stock(raw: Decimal) -> Decimal:
    value = _quantize_quantity(raw, InvalidIngredient)
    if value < 0:
        raise InvalidIngredient("El stock mínimo no puede ser negativo.")
    return value


def validate_unit_cost(
    raw: Decimal, error: type[InvalidIngredient | InvalidMovement] = InvalidIngredient
) -> Decimal:
    if not raw.is_finite() or raw < 0 or raw > MAX_UNIT_COST:
        raise error("El costo unitario tiene que ser un monto positivo razonable.")
    return raw.quantize(COST_STEP)


def _quantize_quantity(
    raw: Decimal, error: type[InvalidIngredient | InvalidMovement | InvalidRecipe]
) -> Decimal:
    if not raw.is_finite() or abs(raw) > MAX_QUANTITY:
        raise error("La cantidad no es válida.")
    if raw != raw.quantize(QUANTITY_STEP):
        raise error("Las cantidades admiten como máximo tres decimales.")
    return raw.quantize(QUANTITY_STEP)
