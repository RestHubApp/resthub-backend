"""Límites y mensajes del dominio del inventario: insumos, movimientos, recetas y compras.

Complementa `test_inventory_domain.py`: acá se fija el primer valor que se
rechaza y el último que se acepta, el mensaje exacto y las cuentas del costo
ponderado y de las sugerencias de compra con sus redondeos.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.modules.inventory.domain.entities import (
    MAX_NAME_LENGTH,
    MAX_QUANTITY,
    MAX_REASON_LENGTH,
    MAX_UNIT_COST,
    NO_STOCK,
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
    InvalidPurchaseOrderError,
    InvalidRecipeError,
    InvalidSupplierError,
)
from resthub.modules.inventory.domain.purchasing import (
    MAX_LINES,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Receipt,
    Supplier,
    suggest_purchase,
)

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
D = Decimal


def _ingredient(**data: object) -> Ingredient:
    fields: dict[str, object] = {"restaurant_id": 1, "name": "Arroz", "unit": Unit.GRAM, "id": 1}
    fields.update(data)
    return Ingredient(**fields)  # type: ignore[arg-type]


def _movement(kind: MovementKind, quantity: str, **extra: object) -> StockMovement:
    return StockMovement(1, 1, kind, D(quantity), created_by=1, **extra)  # type: ignore[arg-type]


def _line(
    ingredient: int = 1, quantity: str = "10", cost: str = "2.5", **extra: object
) -> PurchaseOrderLine:
    return PurchaseOrderLine(ingredient, D(quantity), D(cost), **extra)  # type: ignore[arg-type]


def _order(*lines: PurchaseOrderLine, **extra: object) -> PurchaseOrder:
    return PurchaseOrder(1, 7, 3, 1, list(lines or (_line(id=1),)), **extra)  # type: ignore[arg-type]


# -- Unidades y tipos -----------------------------------------------------------


def test_units_and_movement_kinds_have_their_label() -> None:
    assert [u.label for u in Unit] == ["gramos", "mililitros", "unidades"]
    assert [k.label for k in MovementKind] == ["Compra", "Consumo", "Merma", "Ajuste"]
    assert str(NO_STOCK) == "0.000"


# -- Insumos --------------------------------------------------------------------


def test_new_ingredient_starts_without_minimum_or_cost_and_active() -> None:
    ingredient = _ingredient(name="  Arroz   extra ")

    assert ingredient.name == "Arroz extra"
    assert (ingredient.min_stock, ingredient.unit_cost, ingredient.is_active) == (
        D("0"),
        D("0"),
        True,
    )
    assert str(ingredient.min_stock) == "0.000"
    assert str(ingredient.unit_cost) == "0.000000"


def test_rename_validates_empty_and_maximum_length() -> None:
    ingredient = _ingredient()

    ingredient.rename("A" * MAX_NAME_LENGTH)
    assert len(ingredient.name) == MAX_NAME_LENGTH
    with pytest.raises(InvalidIngredientError) as too_long:
        ingredient.rename("A" * (MAX_NAME_LENGTH + 1))
    with pytest.raises(InvalidIngredientError) as empty:
        ingredient.rename("  ")
    assert too_long.value.reason == "El nombre del insumo no puede pasar de 80."
    assert empty.value.reason == "El nombre del insumo no puede quedar vacío."


def test_minimum_and_cost_change_with_their_scale() -> None:
    ingredient = _ingredient()

    ingredient.set_min_stock(D("2.5"))
    ingredient.set_unit_cost(D("0.0042"))

    assert str(ingredient.min_stock) == "2.500"
    assert str(ingredient.unit_cost) == "0.004200"
    ingredient.set_min_stock(D("0"))
    assert ingredient.min_stock == 0


@pytest.mark.parametrize("cost", ["-0.000001", "NaN", "Infinity", "100000000"])
def test_out_of_range_unit_cost_is_rejected(cost: str) -> None:
    with pytest.raises(InvalidIngredientError) as error:
        _ingredient().set_unit_cost(D(cost))
    assert error.value.reason == "El costo unitario tiene que ser un monto positivo razonable."


def test_unit_cost_accepts_zero_and_the_cap() -> None:
    ingredient = _ingredient()

    ingredient.set_unit_cost(MAX_UNIT_COST)
    assert ingredient.unit_cost == MAX_UNIT_COST
    ingredient.set_unit_cost(D("0"))
    assert ingredient.unit_cost == 0


@pytest.mark.parametrize(
    ("quantity", "message"),
    [
        ("NaN", "La cantidad no es válida."),
        ("1000000000", "La cantidad no es válida."),
        ("-1000000000", "La cantidad no es válida."),
        ("0.0005", "Las cantidades admiten como máximo tres decimales."),
        ("-0.001", "El stock mínimo no puede ser negativo."),
    ],
)
def test_invalid_minimum_explains_the_reason(quantity: str, message: str) -> None:
    with pytest.raises(InvalidIngredientError) as error:
        _ingredient().set_min_stock(D(quantity))
    assert error.value.reason == message


def test_minimum_accepts_the_maximum_quantity() -> None:
    assert _ingredient(min_stock=MAX_QUANTITY).min_stock == MAX_QUANTITY


def test_weighted_cost_blends_old_with_new() -> None:
    # 5 kg a S/ 4 y llegan 20 kg a S/ 5: (5*4 + 20*5) / 25 = 4.8.
    ingredient = _ingredient(unit_cost=D("4"))

    ingredient.absorb_purchase(D("5"), D("20"), D("5"))

    assert ingredient.unit_cost == D("4.800000")


def test_weighted_cost_keeps_six_decimals() -> None:
    ingredient = _ingredient(unit_cost=D("0.004"))

    ingredient.absorb_purchase(D("1000"), D("2000"), D("0.0045"))

    # (1000*0.004 + 2000*0.0045) / 3000 = 13 / 3000 = 0.00433333…
    assert ingredient.unit_cost == D("0.004333")


def test_with_negative_stock_the_cost_is_the_purchase_cost() -> None:
    ingredient = _ingredient(unit_cost=D("9"))

    ingredient.absorb_purchase(D("-3"), D("10"), D("2"))

    assert ingredient.unit_cost == D("2.000000")


def test_invalid_purchase_cost_does_not_change_the_ingredient() -> None:
    ingredient = _ingredient(unit_cost=D("4"))

    with pytest.raises(InvalidIngredientError):
        ingredient.absorb_purchase(D("0"), D("1"), D("-1"))
    assert ingredient.unit_cost == D("4")


# -- Movimientos ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "quantity", "extra", "message"),
    [
        (
            MovementKind.PURCHASE,
            "0",
            {"unit_cost": D("1")},
            "Una compra tiene que sumar una cantidad positiva.",
        ),
        (MovementKind.PURCHASE, "1", {}, "Una compra necesita su costo unitario."),
        (
            MovementKind.CONSUMPTION,
            "0",
            {"order_id": 1, "order_item_id": 1},
            "Un consumo tiene que restar.",
        ),
        (
            MovementKind.CONSUMPTION,
            "-1",
            {"order_id": 1},
            "Un consumo sale siempre de un plato servido.",
        ),
        (
            MovementKind.CONSUMPTION,
            "-1",
            {"order_item_id": 1},
            "Un consumo sale siempre de un plato servido.",
        ),
        (MovementKind.WASTE, "0", {"reason": "se venció"}, "Una merma tiene que restar."),
        (MovementKind.WASTE, "-1", {"reason": "   "}, "Una merma exige indicar el motivo."),
        (MovementKind.ADJUSTMENT, "0", {"reason": "conteo"}, "Un ajuste de cero no cambia nada."),
        (MovementKind.ADJUSTMENT, "-1", {}, "Un ajuste exige indicar el motivo."),
        (
            MovementKind.ADJUSTMENT,
            "1",
            {"reason": "x" * (MAX_REASON_LENGTH + 1)},
            "El motivo admite 200 caracteres.",
        ),
        (
            MovementKind.PURCHASE,
            "1",
            {"unit_cost": D("-1")},
            "El costo unitario tiene que ser un monto positivo razonable.",
        ),
        (MovementKind.ADJUSTMENT, "Infinity", {"reason": "conteo"}, "La cantidad no es válida."),
    ],
)
def test_each_invalid_movement_explains_the_reason(
    kind: MovementKind, quantity: str, extra: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidMovementError) as error:
        _movement(kind, quantity, **extra)
    assert error.value.reason == message


def test_boundary_movements_are_accepted() -> None:
    purchase = _movement(MovementKind.PURCHASE, "0.001", unit_cost=D("0"))
    adjustment = _movement(MovementKind.ADJUSTMENT, "0.001", reason="x" * MAX_REASON_LENGTH)
    waste = _movement(MovementKind.WASTE, "-0.001", reason="se cayó")

    assert purchase.quantity == D("0.001")
    assert purchase.unit_cost == D("0.000000")
    assert len(adjustment.reason) == MAX_REASON_LENGTH
    assert waste.quantity == D("-0.001")


def test_consumption_stores_the_current_cost_with_six_decimals() -> None:
    consumption = _movement(
        MovementKind.CONSUMPTION, "-150", order_id=3, order_item_id=9, unit_cost=D("0.0042")
    )

    assert str(consumption.unit_cost) == "0.004200"
    assert consumption.reason == ""


# -- Stock y recetas -------------------------------------------------------------------


def test_low_and_negative_stock() -> None:
    ingredient = _ingredient(min_stock=D("10"))

    assert StockLevel(ingredient, D("9.999")).is_low
    assert not StockLevel(ingredient, D("10")).is_low
    assert not StockLevel(ingredient, D("0")).is_negative
    assert StockLevel(ingredient, D("-0.001")).is_negative


def test_recipe_keeps_three_decimals_and_rejects_negatives() -> None:
    assert str(RecipeLine(1, D("150.5")).quantity) == "150.500"

    with pytest.raises(InvalidRecipeError) as negative:
        RecipeLine(1, D("-1"))
    with pytest.raises(InvalidRecipeError) as decimals:
        RecipeLine(1, D("0.0001"))
    assert negative.value.reason == "Cada insumo de la receta necesita una cantidad positiva."
    assert decimals.value.reason == "Las cantidades admiten como máximo tres decimales."


def test_valid_recipe_is_returned_in_its_order() -> None:
    lines = [RecipeLine(2, D("1")), RecipeLine(1, D("2"))]

    assert validate_recipe(iter(lines)) == lines
    assert validate_recipe([]) == []
    with pytest.raises(InvalidRecipeError) as error:
        validate_recipe([RecipeLine(1, D("1")), RecipeLine(1, D("2"))])
    assert error.value.reason == "Un insumo aparece dos veces en la receta; suma las cantidades."


def test_recipe_cost_skips_unknown_ingredients_and_does_not_round() -> None:
    ingredients = {1: _ingredient(unit_cost=D("0.0042"))}

    cost = recipe_cost([RecipeLine(1, D("150")), RecipeLine(99, D("10"))], ingredients)

    assert cost == D("0.63")
    assert recipe_cost([], ingredients) == 0


# -- Proveedores ---------------------------------------------------------------------


def test_supplier_cleans_its_texts() -> None:
    supplier = Supplier(1, "  Mercado   Central ", contact=" Rosa ", phone=" 01 555 ", notes=" x ")

    assert (supplier.name, supplier.contact, supplier.phone, supplier.notes) == (
        "Mercado Central",
        "Rosa",
        "01 555",
        "x",
    )
    assert supplier.is_active


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": "  "}, "El proveedor necesita un nombre."),
        ({"name": "N" * 81}, "El nombre del proveedor admite 80 caracteres como máximo."),
        ({"contact": "C" * 81}, "El contacto admite 80 caracteres como máximo."),
        ({"phone": "9" * 21}, "El teléfono admite 20 caracteres como máximo."),
        ({"notes": "N" * 301}, "La nota admite 300 caracteres como máximo."),
    ],
)
def test_invalid_supplier_explains_the_reason(data: dict[str, str], message: str) -> None:
    fields = {"restaurant_id": 1, "name": "Makro", **data}

    with pytest.raises(InvalidSupplierError) as error:
        Supplier(**fields)  # type: ignore[arg-type]
    assert error.value.reason == message


def test_supplier_accepts_the_maximum_lengths() -> None:
    supplier = Supplier(1, "N" * 80, contact="C" * 80, phone="9" * 20, notes="N" * 300)

    assert (len(supplier.name), len(supplier.phone), len(supplier.notes)) == (80, 20, 300)


# -- Órdenes de compra ----------------------------------------------------------------


def test_purchase_order_status_labels() -> None:
    assert [s.label for s in PurchaseOrderStatus] == [
        "Borrador",
        "Enviada",
        "Recibida",
        "Cancelada",
    ]


def test_line_normalizes_quantity_and_cost() -> None:
    line = _line(quantity="2.5", cost="0.0042")

    assert str(line.quantity) == "2.500"
    assert str(line.unit_cost) == "0.004200"
    assert line.received_total == D("0.00")


@pytest.mark.parametrize(
    ("quantity", "cost", "message"),
    [
        ("0", "1", "La cantidad tiene que ser mayor que cero."),
        ("-1", "1", "La cantidad tiene que ser mayor que cero."),
        ("NaN", "1", "La cantidad tiene que ser mayor que cero."),
        ("1", "-0.01", "El costo no puede ser negativo."),
        ("1", "Infinity", "El costo no puede ser negativo."),
    ],
)
def test_invalid_line_explains_the_reason(quantity: str, cost: str, message: str) -> None:
    with pytest.raises(InvalidPurchaseOrderError) as error:
        _line(quantity=quantity, cost=cost)
    assert error.value.reason == message


def test_zero_cost_line_is_valid() -> None:
    assert _line(cost="0").estimated_total == D("0.00")


def test_order_adds_up_estimated_and_received() -> None:
    order = _order(_line(1, "10", "2.5", id=1), _line(2, "3", "1.335", id=2))

    assert order.estimated_total == D("29.01")
    assert order.received_total == D("0.00")


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([], "La orden necesita al menos un insumo."),
        ([_line(i) for i in range(MAX_LINES + 1)], "Una orden admite hasta 60 insumos."),
        ([_line(1), _line(1)], "Un insumo aparece dos veces en la orden."),
    ],
)
def test_malformed_order_is_rejected(lines: list[PurchaseOrderLine], message: str) -> None:
    with pytest.raises(InvalidPurchaseOrderError) as error:
        PurchaseOrder(1, 7, 3, 1, lines)
    assert error.value.reason == message


def test_order_accepts_sixty_ingredients_and_a_three_hundred_char_note() -> None:
    order = PurchaseOrder(1, 7, 3, 1, [_line(i) for i in range(MAX_LINES)], notes="N" * 300)

    assert len(order.lines) == MAX_LINES
    with pytest.raises(InvalidSupplierError):
        PurchaseOrder(1, 7, 3, 1, [_line()], notes="N" * 301)


def test_draft_is_edited_and_note_is_kept_if_not_given() -> None:
    order = _order(notes="Urgente")

    order.replace_lines([_line(5, "1", "1")], None)
    assert [line.ingredient_id for line in order.lines] == [5]
    assert order.notes == "Urgente"
    order.replace_lines([_line(6, "1", "1")], "  Para  el lunes ")
    assert order.notes == "Para el lunes"


def test_sent_order_can_no_longer_be_edited_or_resent() -> None:
    order = _order()
    order.send(NOW)

    assert (order.status, order.sent_at) == (PurchaseOrderStatus.SENT, NOW)
    with pytest.raises(InvalidPurchaseOrderError) as edit_error:
        order.replace_lines([_line(9)], None)
    with pytest.raises(InvalidPurchaseOrderError) as send_error:
        order.send(NOW)
    assert edit_error.value.reason == "Una orden «Enviada» no se puede editar."
    assert send_error.value.reason == "Una orden «Enviada» no se puede enviar."


def test_receive_records_what_arrived_and_returns_only_what_enters() -> None:
    order = _order(_line(1, "10", "2", id=11), _line(2, "5", "3", id=12), _line(3, "1", "4", id=13))

    stocked = order.receive(
        [Receipt(11, D("12.5"), D("1.9")), Receipt(13, D("0"), D("4"))],
        NOW,
    )

    assert [line.id for line in stocked] == [11]
    first, second, third = order.lines
    assert (first.received_quantity, first.received_unit_cost) == (D("12.500"), D("1.900000"))
    # Lo que no llegó queda en cero, con el costo estimado.
    assert (second.received_quantity, second.received_unit_cost) == (D("0.000"), D("3.000000"))
    assert third.received_quantity == D("0.000")
    assert order.received_total == D("23.75")
    assert (order.status, order.received_at) == (PurchaseOrderStatus.RECEIVED, NOW)


def test_unsent_draft_can_also_be_received() -> None:
    order = _order(_line(1, "2", "1", id=1))

    stocked = order.receive([Receipt(1, D("2"), D("1"))], NOW)

    assert [line.id for line in stocked] == [1]
    assert order.status is PurchaseOrderStatus.RECEIVED
    assert order.sent_at is None


def test_if_nothing_arrived_nothing_enters_stock() -> None:
    order = _order(_line(1, "2", "1", id=1))

    assert order.receive([], NOW) == []
    assert order.lines[0].received_quantity == D("0.000")
    assert order.status is PurchaseOrderStatus.RECEIVED


@pytest.mark.parametrize(
    ("receipt", "message"),
    [
        (Receipt(99, D("1"), D("1")), "La orden no tiene la línea 99."),
        (Receipt(1, D("-0.001"), D("1")), "Lo recibido no puede ser negativo."),
        (Receipt(1, D("NaN"), D("1")), "Lo recibido no puede ser negativo."),
        (Receipt(1, D("1"), D("-1")), "El costo no puede ser negativo."),
    ],
)
def test_invalid_receipt_is_rejected(receipt: Receipt, message: str) -> None:
    order = _order(_line(1, "2", "1", id=1))

    with pytest.raises(InvalidPurchaseOrderError) as error:
        order.receive([receipt], NOW)
    assert error.value.reason == message


@pytest.mark.parametrize("closing", ["receive", "cancel"])
def test_closed_order_is_neither_received_nor_cancelled(closing: str) -> None:
    order = _order()
    if closing == "receive":
        order.receive([], NOW)
    else:
        order.cancel(NOW)
    label = order.status.label

    with pytest.raises(InvalidPurchaseOrderError) as receive_error:
        order.receive([], NOW)
    with pytest.raises(InvalidPurchaseOrderError) as cancel_error:
        order.cancel(NOW)
    assert receive_error.value.reason == f"Una orden «{label}» ya no se recibe."
    assert cancel_error.value.reason == f"Una orden «{label}» ya no se cancela."


def test_cancelling_a_sent_order_records_when() -> None:
    order = _order()
    order.send(NOW)

    order.cancel(NOW)

    assert (order.status, order.cancelled_at) == (PurchaseOrderStatus.CANCELLED, NOW)


# -- Sugerencias de compra ---------------------------------------------------------------


def test_well_covered_is_not_suggested() -> None:
    # Justo en el mínimo y con tres días de consumo exactos: todavía alcanza.
    assert suggest_purchase(1, D("30"), D("30"), D("10"), D("1")) is None
    assert suggest_purchase(1, D("5"), D("0"), D("0"), D("1")) is None


def test_below_minimum_double_the_minimum_is_requested() -> None:
    suggestion = suggest_purchase(1, D("4"), D("10"), D("0"), D("0.5"))

    assert suggestion is not None
    assert suggestion.quantity == D("16.000")
    assert (suggestion.stock, suggestion.min_stock, suggestion.unit_cost) == (
        D("4"),
        D("10"),
        D("0.5"),
    )


def test_with_low_coverage_a_week_of_consumption_is_requested() -> None:
    # Hay 20 y se usan 7.5 por día: menos de tres días. Una semana son 52.5.
    suggestion = suggest_purchase(2, D("20"), D("5"), D("7.5"), D("1"))

    assert suggestion is not None
    assert suggestion.ingredient_id == 2
    assert suggestion.quantity == D("32.500")
    assert suggestion.average_daily_use == D("7.500")


def test_with_negative_stock_the_full_target_is_requested() -> None:
    suggestion = suggest_purchase(1, D("-6"), D("10"), D("1"), D("1"))

    assert suggestion is not None
    assert suggestion.quantity == D("20.000")


def test_quantity_that_rounds_to_zero_is_not_suggested() -> None:
    # Bajo un mínimo de 0.0001, el objetivo (0.0002) redondea a 0.000: no hay
    # nada que pedir aunque el insumo figure por debajo del mínimo.
    assert suggest_purchase(1, D("0"), D("0.0001"), D("0"), D("1")) is None
    suggestion = suggest_purchase(1, D("20"), D("25"), D("0"), D("1"))
    assert suggestion is not None
    assert suggestion.quantity == D("30.000")
