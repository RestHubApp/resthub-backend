"""Propiedades del inventario: costos, reposición, compras, conteos físicos y mermas.

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
from resthub.modules.inventory.domain.packaging import EntryMode, Packaging, QuantityEntry
from resthub.modules.inventory.domain.purchasing import (
    ALERT_DAYS,
    COVER_DAYS,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Receipt,
    suggest_purchase,
)
from resthub.modules.inventory.domain.stock_counts import (
    StockCount,
    StockCountLine,
    difference_report,
)
from resthub.modules.inventory.domain.waste_approvals import needs_approval, waste_value

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


# -- Control de mermas ------------------------------------------------------------------


@given(
    size=quantities(1, 5_000_000),
    empty=quantities(1, 2_000_000),
    extra=quantities(1, 2_000_000),
    first=quantities(0, 6_000_000),
    second=quantities(0, 6_000_000),
)
def test_content_by_weight_stays_within_the_package_and_grows_with_weight(
    size: Decimal, empty: Decimal, extra: Decimal, first: Decimal, second: Decimal
) -> None:
    packaging = Packaging(size=size, full_weight_g=empty + extra, empty_weight_g=empty)
    lighter, heavier = sorted((first, second))

    low = packaging.content_by_weight(lighter)
    high = packaging.content_by_weight(heavier)

    assert Decimal(0) <= low <= high <= packaging.size
    assert packaging.content_by_weight(empty) == Decimal("0.000")
    assert packaging.content_by_weight(empty + extra) == packaging.size


@given(
    rows=st.lists(
        st.tuples(quantities(0, 1_000_000), quantities(0, 1_000_000), costs(1_000_000)),
        min_size=1,
        max_size=12,
    )
)
def test_closing_a_count_leaves_each_counted_stock_and_the_report_adds_up(
    rows: list[tuple[Decimal, Decimal, Decimal]],
) -> None:
    count = StockCount(
        restaurant_id=1,
        number=1,
        created_by=1,
        lines=[
            StockCountLine(ingredient_id=i, opening_stock=expected, expected=expected)
            for i, (expected, _, _) in enumerate(rows)
        ],
    )
    for i, (expected, counted, _) in enumerate(rows):
        count.record(
            i,
            counted,
            QuantityEntry(EntryMode.BASE, counted),
            expected=expected,
            actor_id=1,
            now=NOW,
        )

    adjusted = count.close({i: cost for i, (_, _, cost) in enumerate(rows)}, actor_id=1, now=NOW)
    report = difference_report(count)

    # Lo esperado más el ajuste es lo contado: el stock queda en lo que hay.
    for line in count.lines:
        assert line.expected + (line.difference or 0) == line.counted
    assert all(line.difference for line in adjusted)
    assert report.total_value == report.loss_value + report.gain_value
    values = [line.value for line in report.lines]
    assert values == sorted(values)


@given(quantity=quantities(1), cost=costs(), threshold=costs(100_000_000))
def test_waste_needs_approval_exactly_when_its_value_passes_the_threshold(
    quantity: Decimal, cost: Decimal, threshold: Decimal
) -> None:
    amount = threshold.quantize(Decimal("0.01"))
    value = waste_value(quantity, cost)

    assert needs_approval(value, amount) is (value > amount)
    assert needs_approval(value, None) is False
    assert abs(value - quantity * cost) <= Decimal("0.005")
