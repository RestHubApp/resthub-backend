"""Propiedades del inventario: costo ponderado, costo de receta, reposición y compras.

Hypothesis prueba las cuentas con cantidades y costos de cualquier escala (tres
decimales de cantidad, seis de costo) y comprueba lo que debe valer siempre.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from resthub.modules.inventory.domain.entities import (
    COST_STEP,
    QUANTITY_STEP,
    Ingredient,
    RecipeLine,
    Unit,
    recipe_cost,
)
from resthub.modules.inventory.domain.purchasing import (
    ALERT_DAYS,
    COVER_DAYS,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Receipt,
    suggest_purchase,
)

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)


def quantities(minimum: int = 0, maximum: int = 5_000_000) -> st.SearchStrategy[Decimal]:
    """Cantidades con tres decimales, como el libro de stock."""
    return st.integers(minimum, maximum).map(
        lambda thousandths: Decimal(thousandths) * QUANTITY_STEP
    )


def costs(maximum: int = 50_000_000) -> st.SearchStrategy[Decimal]:
    """Costos por unidad con seis decimales, como las columnas `Numeric(14, 6)`."""
    return st.integers(0, maximum).map(lambda millionths: Decimal(millionths) * COST_STEP)


def ingredient(unit_cost: Decimal) -> Ingredient:
    return Ingredient(restaurant_id=1, name="Arroz", unit=Unit.GRAM, unit_cost=unit_cost)


@given(stock=quantities(1), quantity=quantities(1), old=costs(), new=costs())
def test_weighted_average_cost_stays_between_the_old_and_the_new_cost(
    stock: Decimal, quantity: Decimal, old: Decimal, new: Decimal
) -> None:
    arroz = ingredient(old)
    arroz.absorb_purchase(stock, quantity, new)

    # Un paso de costo de margen: el resultado se redondea a seis decimales.
    assert min(old, new) - COST_STEP <= arroz.unit_cost <= max(old, new) + COST_STEP
    # El valor del inventario se conserva (a menos del redondeo del costo).
    value_before = stock * old + quantity * new
    value_after = (stock + quantity) * arroz.unit_cost
    assert abs(value_after - value_before) <= (stock + quantity) * COST_STEP


@given(
    stock=st.integers(-5_000_000, 0).map(lambda n: Decimal(n) * QUANTITY_STEP),
    quantity=quantities(1),
    old=costs(),
    new=costs(),
)
def test_a_purchase_with_no_stock_left_sets_the_cost_to_its_price(
    stock: Decimal, quantity: Decimal, old: Decimal, new: Decimal
) -> None:
    arroz = ingredient(old)
    arroz.absorb_purchase(stock, quantity, new)

    assert arroz.unit_cost == new


recipe_lines = st.dictionaries(
    st.integers(1, 12), st.tuples(quantities(1, 100_000), costs(10_000_000)), max_size=10
)


@given(recipe=recipe_lines, shuffler=st.randoms())
def test_recipe_cost_is_the_sum_of_its_lines_whatever_their_order(
    recipe: dict[int, tuple[Decimal, Decimal]], shuffler: object
) -> None:
    ingredients = {key: ingredient(cost) for key, (_, cost) in recipe.items()}
    lines = [RecipeLine(key, quantity) for key, (quantity, _) in recipe.items()]
    shuffled = list(lines)
    shuffler.shuffle(shuffled)  # type: ignore[attr-defined]

    expected = sum((quantity * cost for quantity, cost in recipe.values()), Decimal(0))
    assert recipe_cost(lines, ingredients) == expected
    assert recipe_cost(shuffled, ingredients) == expected
    # Partir la receta en dos mitades no cambia el costo.
    half = len(lines) // 2
    assert (
        recipe_cost(lines[:half], ingredients) + recipe_cost(lines[half:], ingredients) == expected
    )


@given(
    stock=quantities(0, 3_000_000), minimum=quantities(0, 3_000_000), daily=quantities(0, 500_000)
)
def test_purchase_suggestion_brings_stock_up_to_the_target_or_says_nothing(
    stock: Decimal, minimum: Decimal, daily: Decimal
) -> None:
    suggestion = suggest_purchase(1, stock, minimum, daily, Decimal("1.5"))

    needs_it = stock < minimum or (daily > 0 and stock < daily * ALERT_DAYS)
    target = max(minimum * 2, daily * COVER_DAYS)
    if suggestion is None:
        # Sin sugerencia: o está bien cubierto, o ya llega al objetivo.
        assert not needs_it or stock >= target - QUANTITY_STEP / 2
    else:
        assert needs_it
        assert suggestion.quantity > 0
        # Lo que se pide deja el stock en el objetivo, a menos del redondeo de milésimas.
        assert abs(stock + suggestion.quantity - target) <= QUANTITY_STEP / 2


@given(
    lines=st.lists(
        st.tuples(quantities(1, 1_000_000), costs(5_000_000)),
        min_size=1,
        max_size=8,
    ),
    delivered=st.data(),
)
def test_purchase_order_totals_add_up_and_receiving_returns_only_what_arrived(
    lines: list[tuple[Decimal, Decimal]], delivered: st.DataObject
) -> None:
    order = PurchaseOrder(
        restaurant_id=1,
        number=1,
        supplier_id=2,
        created_by=3,
        lines=[
            PurchaseOrderLine(
                ingredient_id=position, quantity=quantity, unit_cost=cost, id=position
            )
            for position, (quantity, cost) in enumerate(lines, start=1)
        ],
    )
    assert order.estimated_total == sum((line.estimated_total for line in order.lines), Decimal(0))

    receipts = [
        Receipt(line_id=position, quantity=delivered.draw(quantities(0, 1_000_000)), unit_cost=cost)
        for position, (_, cost) in enumerate(lines, start=1)
    ]
    entered = order.receive(receipts, NOW)

    assert order.status is PurchaseOrderStatus.RECEIVED
    assert {line.ingredient_id for line in entered} == {
        receipt.line_id for receipt in receipts if receipt.quantity > 0
    }
    assert order.received_total == sum((line.received_total for line in order.lines), Decimal(0))
