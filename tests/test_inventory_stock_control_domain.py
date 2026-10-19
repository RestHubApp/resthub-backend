"""Reglas puras del control de mermas: presentaciones, conteos físicos y aprobación.

Cuentas exactas, sin base ni HTTP: la conversión por envases y por peso con sus
límites, el ciclo de un conteo y lo que ajusta al cerrar, el reporte de
diferencias y cuándo una merma pide aprobación.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.modules.inventory.domain.exceptions import (
    InvalidPackagingError,
    InvalidQuantityEntryError,
    InvalidStockCountError,
    InvalidWasteApprovalError,
    StockCountNotOpenError,
    WasteAlreadyDecidedError,
)
from resthub.modules.inventory.domain.packaging import EntryMode, Packaging, QuantityEntry
from resthub.modules.inventory.domain.stock_counts import (
    StockCount,
    StockCountLine,
    StockCountStatus,
    difference_report,
)
from resthub.modules.inventory.domain.waste_approvals import (
    WasteApproval,
    WasteApprovalStatus,
    needs_approval,
    waste_value,
)

D = Decimal
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
# Botella de liquor de 750 ml: 1250 g llena, 500 g vacía.
BOTTLE = Packaging(size=D("750"), label="botella", full_weight_g=D("1250"), empty_weight_g=D("500"))


# -- Presentaciones -----------------------------------------------------------------------


def test_packaging_normalizes_label_and_quantities() -> None:
    packaging = Packaging(size=D("750"), label="  botella   grande ")

    assert packaging.size == D("750.000")
    assert packaging.label == "botella grande"
    assert not packaging.weighable
    assert BOTTLE.weighable


@pytest.mark.parametrize(
    ("size", "full", "empty"),
    [
        (D("0"), None, None),
        (D("-1"), None, None),
        (D("0.0001"), None, None),
        (D("750"), D("1250"), None),
        (D("750"), None, D("500")),
        (D("750"), D("500"), D("500")),
        (D("750"), D("400"), D("500")),
        (D("750"), D("1250"), D("0")),
    ],
)
def test_packaging_rejects_invalid_size_or_weights(
    size: Decimal, full: Decimal | None, empty: Decimal | None
) -> None:
    with pytest.raises(InvalidPackagingError):
        Packaging(size=size, full_weight_g=full, empty_weight_g=empty)


def test_packaging_rejects_a_long_label() -> None:
    with pytest.raises(InvalidPackagingError):
        Packaging(size=D("1"), label="x" * 31)


@pytest.mark.parametrize(
    ("weight", "content"),
    [
        (D("1250"), D("750.000")),
        (D("500"), D("0.000")),
        (D("875"), D("375.000")),
        (D("625"), D("125.000")),
        # La balanza marca más que lleno o menos que vacío: se acota.
        (D("1400"), D("750.000")),
        (D("300"), D("0.000")),
        (D("0"), D("0.000")),
    ],
)
def test_content_by_weight_is_proportional_and_bounded(weight: Decimal, content: Decimal) -> None:
    assert BOTTLE.content_by_weight(weight) == content


def test_content_by_weight_rounds_to_three_decimals() -> None:
    # (600 - 500) / (800 - 500) x 1000 = 333.33...: se redondea a tres decimales.
    packaging = Packaging(size=D("1000"), full_weight_g=D("800"), empty_weight_g=D("500"))

    assert packaging.content_by_weight(D("600")) == D("333.333")


def test_content_by_weight_needs_both_weights() -> None:
    with pytest.raises(InvalidQuantityEntryError):
        Packaging(size=D("750")).content_by_weight(D("900"))


def test_entry_in_base_unit_does_not_need_packaging() -> None:
    assert QuantityEntry(EntryMode.BASE, D("12.5")).to_base(None) == D("12.500")


def test_entry_in_packages_multiplies_by_size() -> None:
    assert QuantityEntry(EntryMode.PACKAGES, D("3")).to_base(BOTTLE) == D("2250.000")
    assert QuantityEntry(EntryMode.PACKAGES, D("0.5")).to_base(BOTTLE) == D("375.000")


def test_entry_by_weight_adds_closed_packages() -> None:
    entry = QuantityEntry(EntryMode.WEIGHT, D("875"), full_packages=D("2"))

    assert entry.to_base(BOTTLE) == D("1875.000")


@pytest.mark.parametrize("mode", [EntryMode.PACKAGES, EntryMode.WEIGHT])
def test_entry_in_packages_or_weight_needs_packaging(mode: EntryMode) -> None:
    with pytest.raises(InvalidQuantityEntryError):
        QuantityEntry(mode, D("1")).to_base(None)


@pytest.mark.parametrize(
    ("mode", "quantity", "full_packages"),
    [
        (EntryMode.BASE, D("-1"), D(0)),
        (EntryMode.BASE, D("1.0001"), D(0)),
        (EntryMode.BASE, D("NaN"), D(0)),
        (EntryMode.WEIGHT, D("900"), D("-1")),
    ],
)
def test_entry_rejects_impossible_quantities(
    mode: EntryMode, quantity: Decimal, full_packages: Decimal
) -> None:
    with pytest.raises(InvalidQuantityEntryError):
        QuantityEntry(mode, quantity, full_packages)


def test_entry_in_packages_rejects_an_overflow_and_rounds_fractions() -> None:
    with pytest.raises(InvalidQuantityEntryError):
        QuantityEntry(EntryMode.PACKAGES, D("999999999")).to_base(BOTTLE)
    half = QuantityEntry(EntryMode.PACKAGES, D("0.5"))

    assert QuantityEntry(EntryMode.PACKAGES, D("2")).quantity == D("2.000")
    assert half.to_base(Packaging(size=D("0.333"))) == D("0.167")


# -- Conteos físicos ----------------------------------------------------------------------


def _count(*stocks: tuple[int, str]) -> StockCount:
    return StockCount(
        restaurant_id=1,
        number=4,
        created_by=7,
        lines=[
            StockCountLine(ingredient_id=i, opening_stock=D(stock), expected=D(stock))
            for i, stock in stocks
        ],
    )


def test_stock_count_needs_lines_without_repeats() -> None:
    with pytest.raises(InvalidStockCountError):
        _count()
    with pytest.raises(InvalidStockCountError):
        _count((1, "10"), (1, "5"))


def test_stock_count_rejects_a_long_note() -> None:
    with pytest.raises(InvalidStockCountError):
        StockCount(
            restaurant_id=1,
            number=1,
            created_by=7,
            lines=[StockCountLine(1, D(0), D(0))],
            note="x" * 301,
        )


def test_recording_keeps_the_entry_and_the_stock_at_that_moment() -> None:
    count = _count((1, "3000"), (2, "50"))
    entry = QuantityEntry(EntryMode.PACKAGES, D("4"))

    line = count.record(1, D("2800"), entry, expected=D("2950"), actor_id=9, now=NOW)

    assert line.counted == D("2800")
    assert line.entry == entry
    assert line.opening_stock == D("3000")
    assert line.expected == D("2950")
    assert (line.counted_by, line.counted_at) == (9, NOW)
    assert count.counted_lines == 1


def test_recording_rejects_unknown_ingredient_and_negative_count() -> None:
    count = _count((1, "10"))
    entry = QuantityEntry(EntryMode.BASE, D("1"))

    with pytest.raises(InvalidStockCountError):
        count.record(99, D("1"), entry, expected=D(0), actor_id=9, now=NOW)
    with pytest.raises(InvalidStockCountError):
        count.record(1, D("-1"), entry, expected=D(0), actor_id=9, now=NOW)


def test_clearing_a_line_returns_it_to_the_opening_snapshot() -> None:
    count = _count((1, "10"))
    count.record(
        1, D("8"), QuantityEntry(EntryMode.BASE, D("8")), expected=D("9"), actor_id=9, now=NOW
    )

    line = count.clear(1)

    assert line.counted is None
    assert line.entry is None
    assert line.expected == D("10")
    assert count.counted_lines == 0


def test_closing_adjusts_counted_minus_stock_when_counted_and_skips_the_rest() -> None:
    count = _count((1, "3000"), (2, "50"), (3, "12"), (4, "5"))
    base = QuantityEntry(EntryMode.BASE, D(0))
    # Se contó 2800 cuando el sistema decía 2950 (ya se habían servido 50).
    count.record(1, D("2800"), base, expected=D("2950"), actor_id=9, now=NOW)
    count.record(2, D("55"), base, expected=D("50"), actor_id=9, now=NOW)
    count.record(3, D("12"), base, expected=D("12"), actor_id=9, now=NOW)

    adjusted = count.close({1: D("0.04"), 2: D("0.5"), 3: D("1")}, actor_id=7, now=NOW)

    assert [(line.ingredient_id, line.difference) for line in adjusted] == [
        (1, D("-150")),
        (2, D("5")),
    ]
    assert count.lines[2].difference == D(0)
    # El 4 no se contó: ni diferencia ni costo.
    assert count.lines[3].difference is None
    assert count.lines[3].unit_cost is None
    assert count.status is StockCountStatus.CLOSED
    assert (count.closed_by, count.closed_at) == (7, NOW)
    assert count.reason == "Conteo físico #4"


def test_closing_without_any_counted_line_is_rejected() -> None:
    count = _count((1, "10"))

    with pytest.raises(InvalidStockCountError):
        count.close({}, actor_id=7, now=NOW)
    assert count.status is StockCountStatus.DRAFT


def test_a_closed_or_cancelled_count_does_not_change() -> None:
    closed = _count((1, "10"))
    closed.record(
        1, D("9"), QuantityEntry(EntryMode.BASE, D("9")), expected=D("10"), actor_id=9, now=NOW
    )
    closed.close({1: D(1)}, actor_id=7, now=NOW)
    cancelled = _count((1, "10"))
    cancelled.cancel(actor_id=7, now=NOW)

    for count in (closed, cancelled):
        with pytest.raises(StockCountNotOpenError):
            count.record(
                1, D("1"), QuantityEntry(EntryMode.BASE, D("1")), expected=D(0), actor_id=9, now=NOW
            )
        with pytest.raises(StockCountNotOpenError):
            count.clear(1)
        with pytest.raises(StockCountNotOpenError):
            count.close({}, actor_id=7, now=NOW)
        with pytest.raises(StockCountNotOpenError):
            count.cancel(actor_id=7, now=NOW)
    assert cancelled.status is StockCountStatus.CANCELLED


def test_difference_report_values_and_orders_by_loss() -> None:
    count = _count((1, "3000"), (2, "50"), (3, "0"), (4, "8"))
    base = QuantityEntry(EntryMode.BASE, D(0))
    count.record(1, D("2800"), base, expected=D("3000"), actor_id=9, now=NOW)
    count.record(2, D("55"), base, expected=D("50"), actor_id=9, now=NOW)
    count.record(3, D("2"), base, expected=D("0"), actor_id=9, now=NOW)
    count.close({1: D("0.04"), 2: D("0.5"), 3: D("1.255")}, actor_id=7, now=NOW)

    report = difference_report(count)

    assert [line.ingredient_id for line in report.lines] == [1, 2, 3]
    liquor, lemon, salt = report.lines
    assert (liquor.difference, liquor.percent, liquor.value) == (D("-200"), D("-6.67"), D("-8.00"))
    assert (lemon.difference, lemon.percent, lemon.value) == (D("5"), D("10.00"), D("2.50"))
    # Sin stock esperado no hay porcentaje; el valor se redondea a céntimos.
    assert (salt.percent, salt.value) == (None, D("2.51"))
    assert report.total_value == D("-2.99")
    assert report.loss_value == D("-8.00")
    assert report.gain_value == D("5.01")


# -- Aprobación de mermas -----------------------------------------------------------------


def test_waste_value_is_quantity_by_cost_in_cents() -> None:
    assert waste_value(D("1500"), D("0.04")) == D("60.00")
    assert waste_value(D("1"), D("0.005")) == D("0.01")


@pytest.mark.parametrize(
    ("value", "threshold", "expected"),
    [
        (D("50.01"), D("50.00"), True),
        (D("50.00"), D("50.00"), False),
        (D("10.00"), D("50.00"), False),
        (D("0.01"), D("0.00"), True),
        (D("0.00"), D("0.00"), False),
        (D("9999.00"), None, False),
    ],
)
def test_waste_needs_approval_only_strictly_above_the_threshold(
    value: Decimal, threshold: Decimal | None, *, expected: bool
) -> None:
    assert needs_approval(value, threshold) is expected


def _approval() -> WasteApproval:
    return WasteApproval(
        restaurant_id=1,
        ingredient_id=3,
        quantity=D("1500"),
        unit_cost=D("0.04"),
        reason="Se rompió la botella",
        created_by=8,
    )


def test_approving_records_who_and_when() -> None:
    approval = _approval()

    approval.approve(actor_id=7, now=NOW)

    assert approval.status is WasteApprovalStatus.APPROVED
    assert (approval.decided_by, approval.decided_at) == (7, NOW)
    assert approval.value == D("60.00")


def test_rejecting_needs_a_reason() -> None:
    approval = _approval()

    with pytest.raises(InvalidWasteApprovalError):
        approval.reject("   ", actor_id=7, now=NOW)
    with pytest.raises(InvalidWasteApprovalError):
        approval.reject("x" * 201, actor_id=7, now=NOW)
    approval.reject("  no   cuadra ", actor_id=7, now=NOW)

    assert approval.status is WasteApprovalStatus.REJECTED
    assert approval.rejection_reason == "no cuadra"


def test_a_decided_waste_is_not_decided_again() -> None:
    approved, rejected = _approval(), _approval()
    approved.approve(actor_id=7, now=NOW)
    rejected.reject("no", actor_id=7, now=NOW)

    for approval in (approved, rejected):
        with pytest.raises(WasteAlreadyDecidedError):
            approval.approve(actor_id=7, now=NOW)
        with pytest.raises(WasteAlreadyDecidedError):
            approval.reject("otra vez", actor_id=7, now=NOW)
