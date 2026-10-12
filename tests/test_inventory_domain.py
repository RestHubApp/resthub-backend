"""Reglas de dominio del inventario, sin base ni servidor."""

from __future__ import annotations

from decimal import Decimal

import pytest

from resthub.modules.inventory.domain.entities import (
    Ingredient,
    MovementKind,
    RecipeLine,
    StockLevel,
    StockMovement,
    Unit,
    recipe_cost,
    validate_recipe,
)
from resthub.modules.inventory.domain.exceptions import (
    InvalidIngredientError,
    InvalidMovementError,
    InvalidRecipeError,
)
from resthub.modules.inventory.domain.purchasing import PurchaseOrderLine


def _arroz(**overrides: object) -> Ingredient:
    fields: dict[str, object] = {
        "restaurant_id": 1,
        "name": "Arroz",
        "unit": Unit.GRAM,
        "min_stock": Decimal(5000),
        "unit_cost": Decimal("0.004"),
        "id": 1,
    }
    fields.update(overrides)
    return Ingredient(**fields)  # type: ignore[arg-type]


def _movement(kind: MovementKind, quantity: str, **extra: object) -> StockMovement:
    return StockMovement(
        restaurant_id=1,
        ingredient_id=1,
        kind=kind,
        quantity=Decimal(quantity),
        created_by=1,
        **extra,  # type: ignore[arg-type]
    )


def test_purchase_weights_the_cost_with_existing_stock() -> None:
    arroz = _arroz()

    # 5 kg a S/ 4 el kilo más 20 kg a S/ 5: el kilo queda en S/ 4.80.
    arroz.absorb_purchase(Decimal(5000), Decimal(20000), Decimal("0.005"))

    assert arroz.unit_cost == Decimal("0.004800")


@pytest.mark.parametrize("before", ["0", "-300"])
def test_without_previous_stock_the_cost_is_the_purchase_cost(before: str) -> None:
    arroz = _arroz()

    arroz.absorb_purchase(Decimal(before), Decimal(1000), Decimal("0.0055"))

    assert arroz.unit_cost == Decimal("0.005500")


def test_alert_strictly_below_the_minimum() -> None:
    arroz = _arroz()

    assert StockLevel(arroz, Decimal("4999.999")).is_low
    assert not StockLevel(arroz, Decimal(5000)).is_low
    assert StockLevel(arroz, Decimal(-1)).is_negative


def test_minimum_cannot_be_negative() -> None:
    with pytest.raises(InvalidIngredientError):
        _arroz(min_stock=Decimal(-1))


@pytest.mark.parametrize(
    ("kind", "quantity", "extra"),
    [
        (MovementKind.PURCHASE, "-1", {"unit_cost": Decimal(1)}),
        (MovementKind.PURCHASE, "10", {}),
        (MovementKind.WASTE, "10", {"reason": "se venció"}),
        (MovementKind.WASTE, "-10", {}),
        (MovementKind.ADJUSTMENT, "0", {"reason": "conteo"}),
        (MovementKind.ADJUSTMENT, "5", {}),
        (MovementKind.CONSUMPTION, "-5", {}),
        (MovementKind.CONSUMPTION, "5", {"order_id": 1, "order_item_id": 1}),
        (MovementKind.PURCHASE, "0.0001", {"unit_cost": Decimal(1)}),
    ],
)
def test_each_movement_kind_has_its_sign_and_data(
    kind: MovementKind, quantity: str, extra: dict[str, object]
) -> None:
    with pytest.raises(InvalidMovementError):
        _movement(kind, quantity, **extra)


def test_valid_movements() -> None:
    purchase = _movement(MovementKind.PURCHASE, "10", unit_cost=Decimal("0.5"))
    waste = _movement(MovementKind.WASTE, "-2", reason="  se   cayó ")
    adjustment = _movement(MovementKind.ADJUSTMENT, "-1.5", reason="conteo")
    consumption = _movement(MovementKind.CONSUMPTION, "-0.25", order_id=3, order_item_id=9)

    assert purchase.quantity == Decimal("10.000")
    assert waste.reason == "se cayó"
    assert adjustment.quantity == Decimal("-1.500")
    assert consumption.order_item_id == 9


def test_recipe_has_no_repeated_ingredients_and_needs_positive_quantities() -> None:
    with pytest.raises(InvalidRecipeError):
        validate_recipe([RecipeLine(1, Decimal(10)), RecipeLine(1, Decimal(5))])
    with pytest.raises(InvalidRecipeError):
        RecipeLine(1, Decimal(0))


def test_recipe_cost() -> None:
    ingredients = {
        1: _arroz(),
        2: _arroz(id=2, name="Pollo", unit_cost=Decimal("0.018")),
    }
    lines = [RecipeLine(1, Decimal(150)), RecipeLine(2, Decimal(200))]

    # 150 g x 0.004 + 200 g x 0.018 = 0.60 + 3.60
    assert recipe_cost(lines, ingredients) == Decimal("4.2")


def test_purchase_line_total_rounds_half_a_cent_up() -> None:
    line = PurchaseOrderLine(ingredient_id=1, quantity=Decimal("1.000"), unit_cost=Decimal("1.005"))

    assert line.estimated_total == Decimal("1.01")
