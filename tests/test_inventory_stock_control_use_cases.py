"""Casos de uso del control de mermas con dobles en memoria.

Compras en envases, el ciclo de un conteo físico (foto, carga, cierre con
ajustes), su reporte e histórico, y la merma grande que espera aprobación.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.pagination import Page
from resthub.modules.inventory.domain.entities import Ingredient, MovementKind, Unit
from resthub.modules.inventory.domain.exceptions import (
    IngredientInactiveError,
    IngredientNotFoundError,
    InvalidMovementError,
    InvalidQuantityEntryError,
    InvalidStockCountError,
    StockCountAlreadyOpenError,
    StockCountNotClosedError,
    StockCountNotFoundError,
    StockCountNotOpenError,
    WasteAlreadyDecidedError,
    WasteApprovalNotFoundError,
)
from resthub.modules.inventory.domain.packaging import EntryMode, Packaging, QuantityEntry
from resthub.modules.inventory.domain.stock_counts import (
    StockCount,
    StockCountScope,
    StockCountStatus,
)
from resthub.modules.inventory.domain.waste_approvals import WasteApproval, WasteApprovalStatus
from resthub.modules.inventory.ports.stock_count_repository import CountedLine, StockCountQuery
from resthub.modules.inventory.ports.waste_approval_repository import WasteApprovalQuery
from resthub.modules.inventory.use_cases.register_movements import (
    RegisterPurchaseCommand,
    RegisterWaste,
    RegisterWasteCommand,
    StockChange,
)
from resthub.modules.inventory.use_cases.stock_counts import (
    CancelStockCount,
    CloseStockCount,
    CountEntry,
    IngredientCountHistory,
    ListStockCounts,
    OpenStockCount,
    OpenStockCountCommand,
    ReadCountDifferences,
    RecordCountLines,
    RecordCountLinesCommand,
)
from resthub.modules.inventory.use_cases.waste_approvals import (
    ApproveWaste,
    ListWasteApprovals,
    PendingWaste,
    RejectWaste,
    ReportWaste,
)
from tests.fakes import FIXED_NOW, FixedClock
from tests.test_inventory_use_cases import ACTOR, OTHER_RESTAURANT, RESTAURANT, InventoryHarness

D = Decimal
COUNTER = 9
THRESHOLD = D("50.00")
BOTTLE = Packaging(size=D("750"), label="botella", full_weight_g=D("1250"), empty_weight_g=D("500"))


# -- Dobles -----------------------------------------------------------------------------


class InMemoryStockCounts:
    def __init__(self) -> None:
        self.rows: dict[int, StockCount] = {}
        self.locked_ids: list[int] = []

    async def add(self, count: StockCount) -> StockCount:
        lines = [
            replace(line, id=100 * (len(self.rows) + 1) + i) for i, line in enumerate(count.lines)
        ]
        stored = replace(count, id=len(self.rows) + 1, lines=lines)
        self.rows[stored.id or 0] = stored
        return stored

    async def get(
        self, restaurant_id: int, count_id: int, *, for_update: bool = False
    ) -> StockCount | None:
        if for_update:
            self.locked_ids.append(count_id)
        found = self.rows.get(count_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def open_count(self, restaurant_id: int) -> StockCount | None:
        return next(
            (
                c
                for c in self.rows.values()
                if c.restaurant_id == restaurant_id and c.status is StockCountStatus.DRAFT
            ),
            None,
        )

    async def save(self, count: StockCount) -> StockCount:
        self.rows[count.id or 0] = count
        return count

    async def search(self, query: StockCountQuery) -> Page[StockCount]:
        items = [
            c
            for c in reversed(self.rows.values())
            if c.restaurant_id == query.restaurant_id
            and (query.status is None or c.status is query.status)
        ]
        return Page(items=items, total=len(items))

    async def next_number(self, restaurant_id: int) -> int:
        return 1 + sum(1 for c in self.rows.values() if c.restaurant_id == restaurant_id)

    async def counted_lines(
        self, restaurant_id: int, ingredient_id: int, limit: int
    ) -> list[CountedLine]:
        found = [
            CountedLine(c.id or 0, c.number, c.closed_at or FIXED_NOW, line)
            for c in reversed(self.rows.values())
            if c.restaurant_id == restaurant_id and c.status is StockCountStatus.CLOSED
            for line in c.lines
            if line.ingredient_id == ingredient_id and line.counted is not None
        ]
        return found[:limit]


class InMemoryWasteApprovals:
    def __init__(self) -> None:
        self.rows: dict[int, WasteApproval] = {}
        self.locked_ids: list[int] = []

    async def add(self, approval: WasteApproval) -> WasteApproval:
        stored = replace(approval, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return replace(stored)

    async def get(
        self, restaurant_id: int, approval_id: int, *, for_update: bool = False
    ) -> WasteApproval | None:
        if for_update:
            self.locked_ids.append(approval_id)
        found = self.rows.get(approval_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def save(self, approval: WasteApproval) -> WasteApproval:
        self.rows[approval.id or 0] = replace(approval)
        return replace(approval)

    async def search(self, query: WasteApprovalQuery) -> Page[WasteApproval]:
        items = [
            a
            for a in self.rows.values()
            if a.restaurant_id == query.restaurant_id
            and (query.status is None or a.status is query.status)
        ]
        return Page(items=items, total=len(items))


class FixedThreshold:
    def __init__(self, amount: Decimal | None) -> None:
        self.amount = amount

    async def threshold(self, restaurant_id: int) -> Decimal | None:
        return self.amount


class ControlHarness(InventoryHarness):
    def __init__(self, threshold: Decimal | None = THRESHOLD) -> None:
        super().__init__()
        self.counts = InMemoryStockCounts()
        self.approvals = InMemoryWasteApprovals()
        self.policy = FixedThreshold(threshold)
        self.clock = FixedClock()

    async def stocked(self, name: str, stock: str, cost: str, **fields: object) -> Ingredient:
        ingredient = await self.ingredient(name, unit_cost=D(cost), **fields)
        await self.purchase()(
            RegisterPurchaseCommand(RESTAURANT, ACTOR, ingredient.id or 0, D(stock), D(cost))
        )
        return ingredient

    async def stock(self, ingredient: Ingredient) -> Decimal:
        return (await self.ledger.stock_of(RESTAURANT, [ingredient.id or 0]))[ingredient.id or 0]

    def open(self) -> OpenStockCount:
        return OpenStockCount(self.counts, self.ingredients, self.ledger, self.activity, self.clock)

    def record(self) -> RecordCountLines:
        return RecordCountLines(self.counts, self.ingredients, self.ledger, self.clock)

    def close(self) -> CloseStockCount:
        return CloseStockCount(
            self.counts, self.ingredients, self.ledger, self.activity, self.clock
        )

    def register_waste(self) -> RegisterWaste:
        return RegisterWaste(self.ingredients, self.ledger, self.activity)

    def report_waste(self) -> ReportWaste:
        return ReportWaste(
            self.ingredients,
            self.approvals,
            self.policy,
            self.register_waste(),
            self.activity,
            self.clock,
        )

    def approve(self) -> ApproveWaste:
        return ApproveWaste(self.approvals, self.register_waste(), self.activity, self.clock)

    def reject(self) -> RejectWaste:
        return RejectWaste(self.ingredients, self.approvals, self.activity, self.clock)


def _base(quantity: str) -> QuantityEntry:
    return QuantityEntry(EntryMode.BASE, D(quantity))


# -- Compras en envases -------------------------------------------------------------------


async def test_purchase_in_packages_enters_base_units_and_keeps_how_it_was_entered() -> None:
    a = ControlHarness()
    liquor = await a.ingredient("Pisco", unit=Unit.MILLILITER, packaging=BOTTLE)

    change = await a.purchase()(
        RegisterPurchaseCommand(
            RESTAURANT,
            ACTOR,
            liquor.id or 0,
            D("6"),
            D("0.04"),
            entry=QuantityEntry(EntryMode.PACKAGES, D("6")),
        )
    )

    assert change.movement.quantity == D("4500.000")
    assert (change.movement.entry_mode, change.movement.entry_quantity) == (
        EntryMode.PACKAGES,
        D("6"),
    )
    assert change.level.stock == D("4500.000")


async def test_purchase_cannot_be_entered_by_weight_nor_in_packages_without_packaging() -> None:
    a = ControlHarness()
    liquor = await a.ingredient("Pisco", packaging=BOTTLE)
    rice = await a.ingredient("Arroz")

    with pytest.raises(InvalidQuantityEntryError):
        await a.purchase()(
            RegisterPurchaseCommand(
                RESTAURANT,
                ACTOR,
                liquor.id or 0,
                D("900"),
                D("0.04"),
                entry=QuantityEntry(EntryMode.WEIGHT, D("900")),
            )
        )
    with pytest.raises(InvalidQuantityEntryError):
        await a.purchase()(
            RegisterPurchaseCommand(
                RESTAURANT,
                ACTOR,
                rice.id or 0,
                D("2"),
                D("0.004"),
                entry=QuantityEntry(EntryMode.PACKAGES, D("2")),
            )
        )
    assert a.ledger.movements == []


# -- Conteos físicos ----------------------------------------------------------------------


async def test_opening_a_count_snapshots_the_stock_of_active_ingredients() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04", packaging=BOTTLE)
    lemon = await a.stocked("Limón", "50", "0.5", unit=Unit.UNIT)
    await a.ingredient("Retirado", is_active=False)
    await a.ingredients.add(Ingredient(OTHER_RESTAURANT, "Ajeno", liquor.unit))

    count = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR, note="  cierre  de mes "))

    assert count.number == 1
    assert count.note == "cierre de mes"
    assert count.created_at == FIXED_NOW
    assert {line.ingredient_id: line.opening_stock for line in count.lines} == {
        liquor.id: D("3000.000"),
        lemon.id: D("50.000"),
    }
    assert all(line.expected == line.opening_stock for line in count.lines)
    assert a.activity.entries[-1] == (
        RESTAURANT,
        ACTOR,
        ActivityKind.STOCK_COUNT_OPENED,
        "Conteo físico #1 (2 insumos)",
    )


async def test_opening_a_selected_count_checks_each_ingredient() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04")
    retired = await a.ingredient("Retirado", is_active=False)
    foreign = await a.ingredients.add(Ingredient(OTHER_RESTAURANT, "Ajeno", liquor.unit))

    with pytest.raises(InvalidStockCountError):
        await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR, StockCountScope.SELECTED))
    with pytest.raises(IngredientNotFoundError):
        await a.open()(
            OpenStockCountCommand(
                RESTAURANT, ACTOR, StockCountScope.SELECTED, [liquor.id or 0, foreign.id or 0]
            )
        )
    with pytest.raises(IngredientInactiveError):
        await a.open()(
            OpenStockCountCommand(RESTAURANT, ACTOR, StockCountScope.SELECTED, [retired.id or 0])
        )
    count = await a.open()(
        OpenStockCountCommand(
            RESTAURANT, ACTOR, StockCountScope.SELECTED, [liquor.id or 0, liquor.id or 0]
        )
    )

    assert [line.ingredient_id for line in count.lines] == [liquor.id]
    assert count.scope is StockCountScope.SELECTED


async def test_only_one_count_is_open_per_restaurant_and_needs_ingredients() -> None:
    a = ControlHarness()
    with pytest.raises(InvalidStockCountError):
        await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))
    await a.stocked("Pisco", "3000", "0.04")
    first = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))

    with pytest.raises(StockCountAlreadyOpenError) as error:
        await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))

    assert error.value.number == first.number
    await CancelStockCount(a.counts, a.activity, a.clock)(RESTAURANT, ACTOR, first.id or 0)
    second = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))
    assert second.number == 2
    assert a.activity.kinds()[-2:] == [
        ActivityKind.STOCK_COUNT_CANCELLED,
        ActivityKind.STOCK_COUNT_OPENED,
    ]


async def test_recording_converts_packages_and_weight_and_compares_with_current_stock() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04", packaging=BOTTLE)
    lemon = await a.stocked("Limón", "50", "0.5")
    count = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))
    # Mientras se cuenta se sirven 200 ml: el sistema ya no espera 3000.
    await a.register_waste()(RegisterWasteCommand(RESTAURANT, ACTOR, liquor.id or 0, D("200"), "x"))
    a.clock.advance(minutes=30)

    saved = await a.record()(
        RecordCountLinesCommand(
            RESTAURANT,
            COUNTER,
            count.id or 0,
            [
                CountEntry(liquor.id or 0, QuantityEntry(EntryMode.WEIGHT, D("875"), D("3"))),
                CountEntry(lemon.id or 0, _base("48")),
            ],
        )
    )

    by_id = {line.ingredient_id: line for line in saved.lines}
    assert by_id[liquor.id or 0].counted == D("2625.000")
    assert by_id[liquor.id or 0].opening_stock == D("3000.000")
    assert by_id[liquor.id or 0].expected == D("2800.000")
    assert by_id[liquor.id or 0].counted_by == COUNTER
    assert by_id[liquor.id or 0].counted_at == a.clock.now()
    assert by_id[lemon.id or 0].counted == D("48")
    assert a.counts.locked_ids == [count.id]

    cleared = await a.record()(
        RecordCountLinesCommand(
            RESTAURANT, COUNTER, count.id or 0, [CountEntry(lemon.id or 0, None)]
        )
    )
    assert cleared.counted_lines == 1


async def test_recording_rejects_repeats_foreign_counts_and_unknown_ingredients() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04")
    count = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))
    record = a.record()

    with pytest.raises(InvalidStockCountError):
        await record(
            RecordCountLinesCommand(
                RESTAURANT,
                ACTOR,
                count.id or 0,
                [CountEntry(liquor.id or 0, _base("1")), CountEntry(liquor.id or 0, _base("2"))],
            )
        )
    with pytest.raises(StockCountNotFoundError):
        await record(
            RecordCountLinesCommand(OTHER_RESTAURANT, ACTOR, count.id or 0, [CountEntry(1, None)])
        )
    with pytest.raises(InvalidStockCountError):
        await record(
            RecordCountLinesCommand(RESTAURANT, ACTOR, count.id or 0, [CountEntry(99, _base("1"))])
        )
    with pytest.raises(InvalidQuantityEntryError):
        await record(
            RecordCountLinesCommand(
                RESTAURANT,
                ACTOR,
                count.id or 0,
                [CountEntry(liquor.id or 0, QuantityEntry(EntryMode.PACKAGES, D("1")))],
            )
        )


async def test_closing_adds_one_adjustment_per_difference_and_ignores_uncounted() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04", packaging=BOTTLE)
    lemon = await a.stocked("Limón", "50", "0.5")
    salt = await a.stocked("Sal", "1000", "0.002")
    rice = await a.stocked("Arroz", "5000", "0.004")
    count = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))
    await a.record()(
        RecordCountLinesCommand(
            RESTAURANT,
            COUNTER,
            count.id or 0,
            [
                CountEntry(liquor.id or 0, QuantityEntry(EntryMode.PACKAGES, D("3.5"))),
                CountEntry(lemon.id or 0, _base("55")),
                CountEntry(salt.id or 0, _base("1000")),
            ],
        )
    )
    # Después de contar se sirve liquor: el cierre no lo confunde con pérdida.
    await a.register_waste()(RegisterWasteCommand(RESTAURANT, ACTOR, liquor.id or 0, D("100"), "x"))
    movements_before = len(a.ledger.movements)

    closed = await a.close()(RESTAURANT, ACTOR, count.id or 0)

    adjustments = a.ledger.movements[movements_before:]
    assert [(m.ingredient_id, m.quantity) for m in adjustments] == [
        (liquor.id, D("-375.000")),
        (lemon.id, D("5.000")),
    ]
    assert all(m.kind is MovementKind.ADJUSTMENT for m in adjustments)
    assert {m.reason for m in adjustments} == {"Conteo físico #1"}
    assert {m.stock_count_id for m in adjustments} == {count.id}
    assert adjustments[0].unit_cost == D("0.04")
    assert await a.stock(liquor) == D("2525.000")
    assert await a.stock(rice) == D("5000")
    assert closed.status is StockCountStatus.CLOSED
    assert a.activity.entries[-1] == (
        RESTAURANT,
        ACTOR,
        ActivityKind.STOCK_COUNT_CLOSED,
        "Conteo físico #1: 3 contados, 2 ajustes, diferencia S/ -12.50",
    )


async def test_a_closed_count_is_not_closed_twice() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04")
    count = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))
    await a.record()(
        RecordCountLinesCommand(
            RESTAURANT, ACTOR, count.id or 0, [CountEntry(liquor.id or 0, _base("2900"))]
        )
    )
    await a.close()(RESTAURANT, ACTOR, count.id or 0)
    movements = len(a.ledger.movements)

    with pytest.raises(StockCountNotOpenError):
        await a.close()(RESTAURANT, ACTOR, count.id or 0)

    assert len(a.ledger.movements) == movements


async def test_difference_report_needs_a_closed_count_and_lists_by_loss() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04")
    lemon = await a.stocked("Limón", "50", "0.5")
    count = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))
    read = ReadCountDifferences(a.counts)
    with pytest.raises(StockCountNotClosedError):
        await read(RESTAURANT, count.id or 0)
    await a.record()(
        RecordCountLinesCommand(
            RESTAURANT,
            ACTOR,
            count.id or 0,
            [CountEntry(lemon.id or 0, _base("55")), CountEntry(liquor.id or 0, _base("2800"))],
        )
    )
    await a.close()(RESTAURANT, ACTOR, count.id or 0)

    _, report = await read(RESTAURANT, count.id or 0)

    assert [(line.ingredient_id, line.value) for line in report.lines] == [
        (liquor.id, D("-8.00")),
        (lemon.id, D("2.50")),
    ]
    assert report.total_value == D("-5.50")
    with pytest.raises(StockCountNotFoundError):
        await read(OTHER_RESTAURANT, count.id or 0)


async def test_history_shows_the_latest_differences_of_an_ingredient() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04")
    for counted in ("2900", "2850", "2850"):
        count = await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))
        await a.record()(
            RecordCountLinesCommand(
                RESTAURANT, ACTOR, count.id or 0, [CountEntry(liquor.id or 0, _base(counted))]
            )
        )
        await a.close()(RESTAURANT, ACTOR, count.id or 0)
    # Un conteo abierto no entra en el histórico.
    await a.open()(OpenStockCountCommand(RESTAURANT, ACTOR))

    ingredient, entries = await IngredientCountHistory(a.counts, a.ingredients)(
        RESTAURANT, liquor.id or 0, limit=2
    )

    assert ingredient.name == "Pisco"
    assert [(e.counted.count_number, e.difference.difference) for e in entries] == [
        (3, D("0.000")),
        (2, D("-50.000")),
    ]
    with pytest.raises(IngredientNotFoundError):
        await IngredientCountHistory(a.counts, a.ingredients)(OTHER_RESTAURANT, liquor.id or 0)
    page = await ListStockCounts(a.counts)(
        StockCountQuery(RESTAURANT, status=StockCountStatus.CLOSED)
    )
    assert page.total == 3


# -- Mermas grandes -----------------------------------------------------------------------


async def test_small_waste_goes_straight_to_the_ledger() -> None:
    a = ControlHarness(threshold=D("50.00"))
    liquor = await a.stocked("Pisco", "3000", "0.04")

    outcome = await a.report_waste()(
        RegisterWasteCommand(RESTAURANT, ACTOR, liquor.id or 0, D("1250"), "Se cayó")
    )

    # 1250 ml a S/ 0.04 son S/ 50.00: justo el monto, no pide aprobación.
    assert isinstance(outcome, StockChange)
    assert outcome.level.stock == D("1750.000")
    assert a.approvals.rows == {}


async def test_big_waste_waits_for_approval_without_touching_stock() -> None:
    a = ControlHarness(threshold=D("50.00"))
    liquor = await a.stocked("Pisco", "3000", "0.04", unit=Unit.MILLILITER)
    movements = len(a.ledger.movements)

    outcome = await a.report_waste()(
        RegisterWasteCommand(RESTAURANT, COUNTER, liquor.id or 0, D("1500"), "  Se rompió  ")
    )

    assert isinstance(outcome, PendingWaste)
    approval = outcome.approval
    assert approval.status is WasteApprovalStatus.PENDING
    assert (approval.quantity, approval.unit_cost, approval.value) == (
        D("1500.000"),
        D("0.04"),
        D("60.00"),
    )
    assert approval.reason == "Se rompió"
    assert approval.created_by == COUNTER
    assert len(a.ledger.movements) == movements
    assert a.activity.entries[-1] == (
        RESTAURANT,
        COUNTER,
        ActivityKind.WASTE_APPROVAL_REQUESTED,
        "1500 ml de Pisco (S/ 60.00): Se rompió",
    )


async def test_without_threshold_no_waste_needs_approval() -> None:
    a = ControlHarness(threshold=None)
    liquor = await a.stocked("Pisco", "3000", "0.04")

    outcome = await a.report_waste()(
        RegisterWasteCommand(RESTAURANT, ACTOR, liquor.id or 0, D("3000"), "Inundación")
    )

    assert isinstance(outcome, StockChange)


@pytest.mark.parametrize(("quantity", "reason"), [("0", "x"), ("-1", "x"), ("1", "   ")])
async def test_reported_waste_is_validated_before_deciding(quantity: str, reason: str) -> None:
    a = ControlHarness(threshold=D("0"))
    liquor = await a.stocked("Pisco", "3000", "0.04")

    with pytest.raises(InvalidMovementError):
        await a.report_waste()(
            RegisterWasteCommand(RESTAURANT, ACTOR, liquor.id or 0, D(quantity), reason)
        )
    assert a.approvals.rows == {}


async def test_approving_moves_the_stock_once() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04")
    pending = await a.report_waste()(
        RegisterWasteCommand(RESTAURANT, COUNTER, liquor.id or 0, D("1500"), "Se rompió")
    )
    assert isinstance(pending, PendingWaste)
    approval_id = pending.approval.id or 0

    approved = await a.approve()(RESTAURANT, ACTOR, approval_id)

    assert approved.status is WasteApprovalStatus.APPROVED
    assert approved.decided_by == ACTOR
    assert approved.decided_at == FIXED_NOW
    movement = a.ledger.movements[-1]
    assert (movement.kind, movement.quantity, movement.id) == (
        MovementKind.WASTE,
        D("-1500.000"),
        approved.movement_id,
    )
    assert await a.stock(liquor) == D("1500.000")
    assert a.approvals.locked_ids == [approval_id]
    assert a.activity.kinds()[-2:] == [ActivityKind.STOCK_WASTE, ActivityKind.WASTE_APPROVED]
    with pytest.raises(WasteAlreadyDecidedError):
        await a.approve()(RESTAURANT, ACTOR, approval_id)
    assert await a.stock(liquor) == D("1500.000")


async def test_rejecting_leaves_the_stock_alone() -> None:
    a = ControlHarness()
    liquor = await a.stocked("Pisco", "3000", "0.04")
    pending = await a.report_waste()(
        RegisterWasteCommand(RESTAURANT, COUNTER, liquor.id or 0, D("1500"), "Se rompió")
    )
    assert isinstance(pending, PendingWaste)

    rejected = await a.reject()(RESTAURANT, ACTOR, pending.approval.id or 0, "No cuadra")

    assert rejected.status is WasteApprovalStatus.REJECTED
    assert rejected.rejection_reason == "No cuadra"
    assert rejected.movement_id is None
    assert await a.stock(liquor) == D("3000.000")
    assert a.activity.entries[-1][2] is ActivityKind.WASTE_REJECTED
    with pytest.raises(WasteApprovalNotFoundError):
        await a.reject()(OTHER_RESTAURANT, ACTOR, pending.approval.id or 0, "x")
    with pytest.raises(WasteApprovalNotFoundError):
        await a.approve()(RESTAURANT, ACTOR, 999)
    pending_only = await ListWasteApprovals(a.approvals)(
        WasteApprovalQuery(RESTAURANT, status=WasteApprovalStatus.PENDING)
    )
    assert pending_only.total == 0
