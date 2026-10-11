"""Casos de uso del inventario con dobles en memoria: insumos, libro, compras y recetas.

Cada caso afirma lo que queda en el libro de stock, cómo queda el costo del
insumo, qué asiento deja en la bitácora y qué le pide al repositorio.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.pagination import Page
from resthub.modules.inventory.domain.entities import (
    Ingredient,
    MovementKind,
    RecipeLine,
    StockMovement,
    Unit,
)
from resthub.modules.inventory.domain.exceptions import (
    DishNotFoundError,
    IngredientInactiveError,
    IngredientNameTakenError,
    IngredientNotFoundError,
    InvalidMovementError,
    InvalidPurchaseOrderError,
    InvalidRecipeError,
    PurchaseOrderNotFoundError,
    SupplierNameTakenError,
    SupplierNotFoundError,
)
from resthub.modules.inventory.domain.purchasing import (
    PurchaseOrder,
    PurchaseOrderStatus,
    Receipt,
    Supplier,
)
from resthub.modules.inventory.ports.dish_directory import Dish
from resthub.modules.inventory.ports.purchasing_repository import (
    IngredientUsage,
    PurchaseOrderQuery,
)
from resthub.modules.inventory.ports.stock_ledger import MovementQuery
from resthub.modules.inventory.use_cases.manage_ingredients import (
    CreateIngredient,
    CreateIngredientCommand,
    ListStock,
    ListStockQuery,
    ReadIngredient,
    UpdateIngredient,
    UpdateIngredientCommand,
)
from resthub.modules.inventory.use_cases.purchasing import (
    ChangePurchaseOrderStatus,
    CreatePurchaseOrder,
    EditPurchaseOrder,
    LineData,
    ListPurchaseOrders,
    ReceivePurchaseOrder,
    SaveSupplier,
    SuggestPurchases,
    SupplierData,
)
from resthub.modules.inventory.use_cases.recipes import (
    DishCost,
    ListDishCosts,
    ReadRecipe,
    ReplaceRecipe,
    ReplaceRecipeCommand,
)
from resthub.modules.inventory.use_cases.register_movements import (
    ListMovements,
    ListMovementsQuery,
    RegisterAdjustment,
    RegisterAdjustmentCommand,
    RegisterPurchase,
    RegisterPurchaseCommand,
    RegisterWaste,
    RegisterWasteCommand,
)
from tests.conftest import RecordingActivity

RESTAURANT = 1
OTHER_RESTAURANT = 2
ACTOR = 5
D = Decimal


# -- Dobles -----------------------------------------------------------------------------


class InMemoryIngredients:
    def __init__(self) -> None:
        self.rows: dict[int, Ingredient] = {}
        self.save_count = 0

    async def add(self, ingredient: Ingredient) -> Ingredient:
        stored = replace(ingredient, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return replace(stored)

    async def get(self, restaurant_id: int, ingredient_id: int) -> Ingredient | None:
        found = self.rows.get(ingredient_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def get_many(
        self, restaurant_id: int, ingredient_ids: Collection[int]
    ) -> dict[int, Ingredient]:
        return {
            i: replace(row)
            for i, row in self.rows.items()
            if i in ingredient_ids and row.restaurant_id == restaurant_id
        }

    async def find_by_name(self, restaurant_id: int, name: str) -> Ingredient | None:
        return next(
            (
                replace(r)
                for r in self.rows.values()
                if r.restaurant_id == restaurant_id and r.name.casefold() == name.casefold()
            ),
            None,
        )

    async def list_all(self, restaurant_id: int) -> list[Ingredient]:
        return [replace(r) for r in self.rows.values() if r.restaurant_id == restaurant_id]

    async def save(self, ingredient: Ingredient) -> Ingredient:
        self.save_count += 1
        self.rows[ingredient.id or 0] = replace(ingredient)
        return replace(ingredient)


class InMemoryLedger:
    def __init__(self) -> None:
        self.movements: list[StockMovement] = []
        self.queries: list[MovementQuery] = []

    async def add(self, movement: StockMovement) -> StockMovement:
        stored = replace(movement, id=len(self.movements) + 1)
        self.movements.append(stored)
        return stored

    async def add_many(self, movements: list[StockMovement]) -> list[StockMovement]:
        return [await self.add(m) for m in movements]

    async def stock_of(
        self, restaurant_id: int, ingredient_ids: Collection[int] | None = None
    ) -> dict[int, Decimal]:
        stock: dict[int, Decimal] = {}
        for m in self.movements:
            if m.restaurant_id == restaurant_id and (
                ingredient_ids is None or m.ingredient_id in ingredient_ids
            ):
                stock[m.ingredient_id] = stock.get(m.ingredient_id, D("0.000")) + m.quantity
        return stock

    async def search(self, query: MovementQuery) -> Page[StockMovement]:
        self.queries.append(query)
        items = [m for m in reversed(self.movements) if m.restaurant_id == query.restaurant_id]
        return Page(items=items, total=len(items))

    async def consumed_order_items(self, restaurant_id: int, ids: Collection[int]) -> set[int]:
        return set()


class InMemoryOrders:
    def __init__(self, order_numbers: dict[int, int]) -> None:
        self.order_numbers = order_numbers

    async def numbers(self, restaurant_id: int, order_ids: Collection[int]) -> dict[int, int]:
        return {k: v for k, v in self.order_numbers.items() if k in order_ids}


class InMemorySuppliers:
    def __init__(self) -> None:
        self.rows: dict[int, Supplier] = {}

    async def add(self, supplier: Supplier) -> Supplier:
        stored = replace(supplier, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return replace(stored)

    async def get(self, restaurant_id: int, supplier_id: int) -> Supplier | None:
        found = self.rows.get(supplier_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def find_by_name(self, restaurant_id: int, name: str) -> Supplier | None:
        return next(
            (
                replace(s)
                for s in self.rows.values()
                if s.restaurant_id == restaurant_id and s.name.casefold() == name.casefold()
            ),
            None,
        )

    async def list_all(self, restaurant_id: int) -> list[Supplier]:
        return [replace(s) for s in self.rows.values() if s.restaurant_id == restaurant_id]

    async def save(self, supplier: Supplier) -> Supplier:
        self.rows[supplier.id or 0] = replace(supplier)
        return replace(supplier)


class InMemoryPurchaseOrders:
    def __init__(self) -> None:
        self.rows: dict[int, PurchaseOrder] = {}
        self.locked_ids: list[int] = []
        self.queries: list[PurchaseOrderQuery] = []

    async def add(self, order: PurchaseOrder) -> PurchaseOrder:
        lines = [
            replace(line, id=10 * (len(self.rows) + 1) + i) for i, line in enumerate(order.lines)
        ]
        stored = replace(order, id=len(self.rows) + 1, lines=lines)
        self.rows[stored.id or 0] = stored
        return stored

    async def get(
        self, restaurant_id: int, order_id: int, *, for_update: bool = False
    ) -> PurchaseOrder | None:
        if for_update:
            self.locked_ids.append(order_id)
        found = self.rows.get(order_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def save(self, order: PurchaseOrder) -> PurchaseOrder:
        self.rows[order.id or 0] = order
        return order

    async def search(self, query: PurchaseOrderQuery) -> Page[PurchaseOrder]:
        self.queries.append(query)
        items = [o for o in self.rows.values() if o.restaurant_id == query.restaurant_id]
        return Page(items=items, total=len(items))

    async def next_number(self, restaurant_id: int) -> int:
        return 1 + sum(1 for o in self.rows.values() if o.restaurant_id == restaurant_id)


class InMemoryRecipes:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, int], list[RecipeLine]] = {}

    async def lines_for(self, restaurant_id: int, menu_item_id: int) -> list[RecipeLine]:
        return list(self.rows.get((restaurant_id, menu_item_id), []))

    async def lines_for_many(self, restaurant_id: int) -> dict[int, list[RecipeLine]]:
        return {dish: lines for (rid, dish), lines in self.rows.items() if rid == restaurant_id}

    async def replace(self, restaurant_id: int, menu_item_id: int, lines: list[RecipeLine]) -> None:
        if lines:
            self.rows[(restaurant_id, menu_item_id)] = list(lines)
        else:
            self.rows.pop((restaurant_id, menu_item_id), None)


class InMemoryMenu:
    def __init__(self, *dishes: Dish) -> None:
        self.dishes = list(dishes)

    async def get(self, restaurant_id: int, menu_item_id: int) -> Dish | None:
        return next((p for p in self.dishes if p.id == menu_item_id), None)

    async def list_all(self, restaurant_id: int) -> list[Dish]:
        return list(self.dishes)


class InMemoryUsage:
    def __init__(self, *rows: IngredientUsage) -> None:
        self.rows = list(rows)
        self.since_calls: list[datetime] = []

    async def usage_since(self, restaurant_id: int, since: datetime) -> list[IngredientUsage]:
        self.since_calls.append(since)
        return list(self.rows)


class InventoryHarness:
    def __init__(self) -> None:
        self.ingredients = InMemoryIngredients()
        self.ledger = InMemoryLedger()
        self.activity = RecordingActivity()

    async def ingredient(self, name: str = "Arroz", **fields: object) -> Ingredient:
        values: dict[str, object] = {"restaurant_id": RESTAURANT, "name": name, "unit": Unit.GRAM}
        values.update(fields)
        return await self.ingredients.add(Ingredient(**values))  # type: ignore[arg-type]

    def purchase(self) -> RegisterPurchase:
        return RegisterPurchase(self.ingredients, self.ledger, self.activity)


# -- Insumos ----------------------------------------------------------------------------


async def test_creating_an_ingredient_logs_it_with_no_stock() -> None:
    a = InventoryHarness()

    level = await CreateIngredient(a.ingredients, a.ledger, a.activity)(
        CreateIngredientCommand(RESTAURANT, ACTOR, "  Arroz  ", Unit.GRAM, D("5000"), D("0.0042"))
    )

    assert (level.ingredient.name, level.ingredient.min_stock, level.stock) == (
        "Arroz",
        D("5000.000"),
        D("0.000"),
    )
    assert level.ingredient.unit_cost == D("0.004200")
    assert a.activity.entries == [
        (RESTAURANT, ACTOR, ActivityKind.INGREDIENT_CREATED, "Arroz (gramos)")
    ]


async def test_ingredient_name_is_unique_within_the_restaurant() -> None:
    a = InventoryHarness()
    await a.ingredient("Arroz")
    await a.ingredients.add(Ingredient(OTHER_RESTAURANT, "Azúcar", Unit.GRAM))

    with pytest.raises(IngredientNameTakenError) as error:
        await CreateIngredient(a.ingredients, a.ledger, a.activity)(
            CreateIngredientCommand(RESTAURANT, ACTOR, "ARROZ", Unit.GRAM)
        )
    other_restaurant = await CreateIngredient(a.ingredients, a.ledger, a.activity)(
        CreateIngredientCommand(RESTAURANT, ACTOR, "Azúcar", Unit.GRAM)
    )
    assert error.value.name == "ARROZ"
    assert other_restaurant.ingredient.restaurant_id == RESTAURANT


async def test_updating_an_ingredient_changes_only_what_was_sent() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient(min_stock=D("10"), unit_cost=D("0.004"))
    await a.purchase()(
        RegisterPurchaseCommand(RESTAURANT, ACTOR, arroz.id or 0, D("100"), D("0.004"))
    )
    edit = UpdateIngredient(a.ingredients, a.ledger, a.activity)

    level = await edit(
        UpdateIngredientCommand(RESTAURANT, ACTOR, arroz.id or 0, name=" Arroz  extra ")
    )
    assert (level.ingredient.name, level.ingredient.min_stock, level.stock) == (
        "Arroz extra",
        D("10.000"),
        D("100.000"),
    )
    level = await edit(
        UpdateIngredientCommand(
            RESTAURANT,
            ACTOR,
            arroz.id or 0,
            min_stock=D("20"),
            unit_cost=D("0.005"),
            is_active=False,
        )
    )
    saved = a.ingredients.rows[arroz.id or 0]
    assert (saved.name, saved.min_stock, saved.unit_cost, saved.is_active) == (
        "Arroz extra",
        D("20.000"),
        D("0.005000"),
        False,
    )
    assert level.ingredient.is_active is False
    assert [e[2:] for e in a.activity.entries[-2:]] == [
        (ActivityKind.INGREDIENT_UPDATED, "Arroz extra"),
        (ActivityKind.INGREDIENT_UPDATED, "Arroz extra"),
    ]


async def test_updating_an_ingredient_may_keep_its_name_but_not_take_another() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient("Arroz")
    await a.ingredient("Pollo")
    edit = UpdateIngredient(a.ingredients, a.ledger, a.activity)

    await edit(UpdateIngredientCommand(RESTAURANT, ACTOR, arroz.id or 0, name="ARROZ"))
    with pytest.raises(IngredientNameTakenError):
        await edit(UpdateIngredientCommand(RESTAURANT, ACTOR, arroz.id or 0, name="pollo"))
    with pytest.raises(IngredientNotFoundError) as foreign:
        await edit(UpdateIngredientCommand(OTHER_RESTAURANT, ACTOR, arroz.id or 0, name="X"))
    assert a.ingredients.rows[arroz.id or 0].name == "ARROZ"
    assert foreign.value.ingredient_id == arroz.id


async def test_reading_an_ingredient_returns_its_stock() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient()
    await a.ingredient("Pollo")
    await a.purchase()(RegisterPurchaseCommand(RESTAURANT, ACTOR, arroz.id or 0, D("7.5"), D("1")))

    level = await ReadIngredient(a.ingredients, a.ledger)(RESTAURANT, arroz.id or 0)
    no_movements = await ReadIngredient(a.ingredients, a.ledger)(RESTAURANT, 2)

    assert level.stock == D("7.500")
    assert str(no_movements.stock) == "0.000"


async def test_stock_lists_active_items_and_alerts_sort_the_most_urgent_first() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient("Arroz", min_stock=D("100"))
    chicken = await a.ingredient("Pollo", min_stock=D("50"))
    salt = await a.ingredient("Sal", min_stock=D("10"))
    retired = await a.ingredient("Retirado", min_stock=D("10"), is_active=False)
    await a.ledger.add(
        StockMovement(RESTAURANT, arroz.id or 0, MovementKind.ADJUSTMENT, D("90"), 1, reason="x")
    )
    await a.ledger.add(
        StockMovement(RESTAURANT, chicken.id or 0, MovementKind.ADJUSTMENT, D("-5"), 1, reason="x")
    )
    await a.ledger.add(
        StockMovement(RESTAURANT, salt.id or 0, MovementKind.ADJUSTMENT, D("10"), 1, reason="x")
    )
    list_stock = ListStock(a.ingredients, a.ledger)

    active_items = await list_stock(ListStockQuery(RESTAURANT))
    all_items = await list_stock(ListStockQuery(RESTAURANT, include_inactive=True))
    alerts = await list_stock(ListStockQuery(RESTAURANT, include_inactive=True, low_only=True))

    assert [n.ingredient.name for n in active_items] == ["Arroz", "Pollo", "Sal"]
    assert [n.stock for n in active_items] == [D("90.000"), D("-5.000"), D("10.000")]
    assert [n.ingredient.id for n in all_items][-1] == retired.id
    # Primero lo negativo; después lo que más lejos está del mínimo. El retirado no alerta.
    assert [n.ingredient.name for n in alerts] == ["Pollo", "Arroz"]


async def test_alerts_without_negatives_sort_by_distance_to_minimum() -> None:
    a = InventoryHarness()
    near = await a.ingredient("Cerca", min_stock=D("10"))
    far = await a.ingredient("Lejos", min_stock=D("100"))
    await a.ledger.add(
        StockMovement(RESTAURANT, near.id or 0, MovementKind.ADJUSTMENT, D("9"), 1, reason="x")
    )
    await a.ledger.add(
        StockMovement(RESTAURANT, far.id or 0, MovementKind.ADJUSTMENT, D("1"), 1, reason="x")
    )

    alerts = await ListStock(a.ingredients, a.ledger)(ListStockQuery(RESTAURANT, low_only=True))

    assert [n.ingredient.name for n in alerts] == ["Lejos", "Cerca"]


# -- Libro de movimientos -------------------------------------------------------------------


async def test_purchase_enters_the_ledger_and_weights_the_cost() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient(unit_cost=D("4"), unit=Unit.UNIT)
    purchase = a.purchase()
    await purchase(RegisterPurchaseCommand(RESTAURANT, ACTOR, arroz.id or 0, D("5"), D("4")))

    result = await purchase(
        RegisterPurchaseCommand(RESTAURANT, ACTOR, arroz.id or 0, D("20"), D("5"), reason="Makro")
    )

    assert (result.movement.kind, result.movement.quantity, result.movement.unit_cost) == (
        MovementKind.PURCHASE,
        D("20.000"),
        D("5.000000"),
    )
    assert (result.movement.reason, result.movement.created_by) == ("Makro", ACTOR)
    assert result.level.stock == D("25.000")
    assert a.ingredients.rows[arroz.id or 0].unit_cost == D("4.800000")
    assert result.level.ingredient.unit_cost == D("4.800000")
    assert a.activity.entries[-1] == (
        RESTAURANT,
        ACTOR,
        ActivityKind.STOCK_PURCHASE,
        "20 unit de Arroz",
    )


async def test_purchase_log_shows_quantity_without_extra_zeros() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient()

    await a.purchase()(
        RegisterPurchaseCommand(RESTAURANT, ACTOR, arroz.id or 0, D("1.500"), D("1"))
    )
    await a.purchase()(RegisterPurchaseCommand(RESTAURANT, ACTOR, arroz.id or 0, D("1000"), D("1")))

    assert [e[3] for e in a.activity.entries] == ["1.5 g de Arroz", "1000 g de Arroz"]


async def test_retired_or_foreign_ingredient_cannot_be_purchased_or_adjusted() -> None:
    a = InventoryHarness()
    retired = await a.ingredient("Retirado", is_active=False)
    foreign = await a.ingredients.add(Ingredient(OTHER_RESTAURANT, "Ajeno", Unit.GRAM))

    with pytest.raises(IngredientInactiveError) as inactive:
        await a.purchase()(
            RegisterPurchaseCommand(RESTAURANT, ACTOR, retired.id or 0, D("1"), D("1"))
        )
    with pytest.raises(IngredientNotFoundError):
        await a.purchase()(
            RegisterPurchaseCommand(RESTAURANT, ACTOR, foreign.id or 0, D("1"), D("1"))
        )
    assert inactive.value.name == "Retirado"
    assert a.ledger.movements == []
    assert a.ingredients.save_count == 0


async def test_waste_subtracts_at_current_cost_and_explains_the_reason() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient(unit_cost=D("0.004"))
    await a.purchase()(
        RegisterPurchaseCommand(RESTAURANT, ACTOR, arroz.id or 0, D("1000"), D("0.004"))
    )

    result = await RegisterWaste(a.ingredients, a.ledger, a.activity)(
        RegisterWasteCommand(RESTAURANT, ACTOR, arroz.id or 0, D("250"), "  se   quemó ")
    )

    assert (result.movement.kind, result.movement.quantity) == (MovementKind.WASTE, D("-250.000"))
    assert result.movement.unit_cost == D("0.004000")
    assert result.movement.created_by == ACTOR
    assert result.level.stock == D("750.000")
    assert a.activity.entries[-1] == (
        RESTAURANT,
        ACTOR,
        ActivityKind.STOCK_WASTE,
        "250 g de Arroz: se quemó",
    )


@pytest.mark.parametrize("quantity", ["0", "-1"])
async def test_waste_is_given_as_a_positive_amount(quantity: str) -> None:
    a = InventoryHarness()
    arroz = await a.ingredient()

    with pytest.raises(InvalidMovementError) as error:
        await RegisterWaste(a.ingredients, a.ledger, a.activity)(
            RegisterWasteCommand(RESTAURANT, ACTOR, arroz.id or 0, D(quantity), "se cayó")
        )
    assert error.value.reason == "Indica cuánto se perdió, en positivo."
    assert a.ledger.movements == []


async def test_adjustment_by_difference_adds_with_sign() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient(unit_cost=D("2"))

    result = await RegisterAdjustment(a.ingredients, a.ledger, a.activity)(
        RegisterAdjustmentCommand(RESTAURANT, ACTOR, arroz.id or 0, "conteo", quantity=D("-3"))
    )

    assert (result.movement.kind, result.movement.quantity, result.movement.unit_cost) == (
        MovementKind.ADJUSTMENT,
        D("-3.000"),
        D("2.000000"),
    )
    assert result.movement.created_by == ACTOR
    assert result.level.stock == D("-3.000")
    assert a.activity.entries[-1] == (
        RESTAURANT,
        ACTOR,
        ActivityKind.STOCK_ADJUSTMENT,
        "-3 g de Arroz: conteo",
    )


async def test_adjustment_by_count_computes_the_difference_against_the_ledger() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient()
    await a.purchase()(RegisterPurchaseCommand(RESTAURANT, ACTOR, arroz.id or 0, D("10"), D("1")))
    adjust = RegisterAdjustment(a.ingredients, a.ledger, a.activity)

    result = await adjust(
        RegisterAdjustmentCommand(
            RESTAURANT, ACTOR, arroz.id or 0, "inventario", counted_stock=D("7.5")
        )
    )
    to_zero = await adjust(
        RegisterAdjustmentCommand(
            RESTAURANT, ACTOR, arroz.id or 0, "se acabó", counted_stock=D("0")
        )
    )

    assert result.movement.quantity == D("-2.500")
    assert result.level.stock == D("7.500")
    assert to_zero.movement.quantity == D("-7.500")
    assert to_zero.level.stock == D("0.000")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({}, "Indica la diferencia o el stock contado, uno de los dos."),
        (
            {"quantity": D("1"), "counted_stock": D("1")},
            "Indica la diferencia o el stock contado, uno de los dos.",
        ),
        ({"counted_stock": D("-0.001")}, "El stock contado no puede ser negativo."),
    ],
)
async def test_badly_requested_adjustment_is_rejected(
    fields: dict[str, Decimal], message: str
) -> None:
    a = InventoryHarness()
    arroz = await a.ingredient()

    with pytest.raises(InvalidMovementError) as error:
        await RegisterAdjustment(a.ingredients, a.ledger, a.activity)(
            RegisterAdjustmentCommand(RESTAURANT, ACTOR, arroz.id or 0, "conteo", **fields)
        )
    assert error.value.reason == message
    assert a.ledger.movements == []


async def test_adjustment_on_a_foreign_ingredient_does_not_exist() -> None:
    a = InventoryHarness()
    foreign = await a.ingredients.add(Ingredient(OTHER_RESTAURANT, "Ajeno", Unit.GRAM))

    with pytest.raises(IngredientNotFoundError):
        await RegisterAdjustment(a.ingredients, a.ledger, a.activity)(
            RegisterAdjustmentCommand(RESTAURANT, ACTOR, foreign.id or 0, "conteo", quantity=D("1"))
        )
    with pytest.raises(IngredientNotFoundError):
        await RegisterWaste(a.ingredients, a.ledger, a.activity)(
            RegisterWasteCommand(RESTAURANT, ACTOR, foreign.id or 0, D("1"), "x")
        )


async def test_ledger_returns_ingredient_and_order_number_for_each_movement() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient()
    await a.ledger.add(
        StockMovement(
            RESTAURANT, arroz.id or 0, MovementKind.PURCHASE, D("10"), 1, unit_cost=D("1")
        )
    )
    await a.ledger.add(
        StockMovement(
            RESTAURANT,
            arroz.id or 0,
            MovementKind.CONSUMPTION,
            D("-1"),
            1,
            order_id=40,
            order_item_id=4,
        )
    )
    await a.ledger.add(
        StockMovement(RESTAURANT, 999, MovementKind.ADJUSTMENT, D("1"), 1, reason="x")
    )

    page = await ListMovements(a.ingredients, a.ledger, InMemoryOrders({40: 14}))(
        ListMovementsQuery(
            RESTAURANT,
            ingredient_id=arroz.id,
            kinds=frozenset({MovementKind.CONSUMPTION}),
            order_id=40,
            limit=5,
            offset=1,
        )
    )

    assert [(e.movement.kind, e.ingredient.name, e.order_number) for e in page.items] == [
        (MovementKind.CONSUMPTION, "Arroz", 14),
        (MovementKind.PURCHASE, "Arroz", None),
    ]
    assert page.total == 3
    assert a.ledger.queries == [
        MovementQuery(
            restaurant_id=RESTAURANT,
            ingredient_id=arroz.id,
            kinds=frozenset({MovementKind.CONSUMPTION}),
            order_id=40,
            limit=5,
            offset=1,
        )
    ]


# -- Proveedores y órdenes de compra -----------------------------------------------------------


async def test_creating_and_editing_a_supplier() -> None:
    suppliers, activity = InMemorySuppliers(), RecordingActivity()
    save = SaveSupplier(suppliers, activity)

    created = await save(
        RESTAURANT, ACTOR, SupplierData("Makro", contact="Rosa", phone="999", notes="lunes")
    )
    edited = await save(
        RESTAURANT,
        ACTOR,
        SupplierData("Makro Surco", contact="Luis", phone="888", notes="martes", is_active=False),
        created.id,
    )

    assert (created.name, created.contact, created.phone, created.notes, created.is_active) == (
        "Makro",
        "Rosa",
        "999",
        "lunes",
        True,
    )
    assert edited.id == created.id
    assert edited.created_at == created.created_at
    saved = suppliers.rows[created.id or 0]
    assert (
        saved.name,
        saved.contact,
        saved.phone,
        saved.notes,
        saved.is_active,
    ) == (
        "Makro Surco",
        "Luis",
        "888",
        "martes",
        False,
    )
    assert activity.entries == [
        (RESTAURANT, ACTOR, ActivityKind.SUPPLIER_CREATED, "Makro"),
        (RESTAURANT, ACTOR, ActivityKind.SUPPLIER_UPDATED, "Makro Surco"),
    ]


async def test_supplier_name_is_unique_but_may_keep_its_own() -> None:
    suppliers, activity = InMemorySuppliers(), RecordingActivity()
    save = SaveSupplier(suppliers, activity)
    makro = await save(RESTAURANT, ACTOR, SupplierData("Makro"))
    await save(RESTAURANT, ACTOR, SupplierData("Tottus"))

    await save(RESTAURANT, ACTOR, SupplierData("MAKRO"), makro.id)
    with pytest.raises(SupplierNameTakenError) as duplicate:
        await save(RESTAURANT, ACTOR, SupplierData("tottus"), makro.id)
    with pytest.raises(SupplierNameTakenError):
        await save(RESTAURANT, ACTOR, SupplierData("Tottus"))
    with pytest.raises(SupplierNotFoundError) as foreign:
        await save(OTHER_RESTAURANT, ACTOR, SupplierData("Makro"), makro.id)

    assert duplicate.value.name == "tottus"
    assert foreign.value.supplier_id == makro.id
    assert suppliers.rows[makro.id or 0].name == "MAKRO"
    assert len(activity.entries) == 3


class PurchasingHarness:
    def __init__(self) -> None:
        self.warehouse = InventoryHarness()
        self.suppliers = InMemorySuppliers()
        self.purchase_orders = InMemoryPurchaseOrders()
        self.activity = self.warehouse.activity

    async def seed(self) -> tuple[Supplier, Ingredient, Ingredient]:
        makro = await self.suppliers.add(Supplier(RESTAURANT, "Makro"))
        arroz = await self.warehouse.ingredient("Arroz", unit_cost=D("4"), unit=Unit.UNIT)
        chicken = await self.warehouse.ingredient("Pollo", unit_cost=D("10"), unit=Unit.UNIT)
        return makro, arroz, chicken

    def create(self) -> CreatePurchaseOrder:
        return CreatePurchaseOrder(
            self.purchase_orders, self.suppliers, self.warehouse.ingredients, self.activity
        )

    def receive(self) -> ReceivePurchaseOrder:
        return ReceivePurchaseOrder(
            self.purchase_orders, self.suppliers, self.warehouse.purchase(), self.activity
        )


async def test_creating_an_order_numbers_it_per_restaurant_and_logs_it() -> None:
    c = PurchasingHarness()
    makro, arroz, chicken = await c.seed()

    purchase_order = await c.create()(
        RESTAURANT,
        ACTOR,
        makro.id or 0,
        [LineData(arroz.id or 0, D("10"), D("4.5")), LineData(chicken.id or 0, D("2"), D("10"))],
        notes="Para el viernes",
    )
    second = await c.create()(
        RESTAURANT, ACTOR, makro.id or 0, [LineData(arroz.id or 0, D("1"), D("1"))]
    )

    assert (
        purchase_order.number,
        purchase_order.supplier_id,
        purchase_order.created_by,
        purchase_order.notes,
    ) == (
        1,
        makro.id,
        ACTOR,
        "Para el viernes",
    )
    assert [(ln.ingredient_id, ln.quantity, ln.unit_cost) for ln in purchase_order.lines] == [
        (arroz.id, D("10.000"), D("4.500000")),
        (chicken.id, D("2.000"), D("10.000000")),
    ]
    assert second.number == 2
    assert second.notes == ""
    assert c.activity.entries[0] == (
        RESTAURANT,
        ACTOR,
        ActivityKind.PURCHASE_ORDER_CREATED,
        "OC 1 a Makro (S/ 65.00)",
    )


async def test_order_is_not_built_with_a_foreign_or_retired_supplier_or_ingredient() -> None:
    c = PurchasingHarness()
    makro, arroz, _ = await c.seed()
    retired = await c.warehouse.ingredient("Retirado", is_active=False)
    foreign = await c.suppliers.add(Supplier(OTHER_RESTAURANT, "Ajeno"))
    line = [LineData(arroz.id or 0, D("1"), D("1"))]

    with pytest.raises(SupplierNotFoundError):
        await c.create()(RESTAURANT, ACTOR, foreign.id or 0, line)
    with pytest.raises(IngredientInactiveError) as inactive:
        await c.create()(
            RESTAURANT, ACTOR, makro.id or 0, [LineData(retired.id or 0, D("1"), D("1"))]
        )
    with pytest.raises(IngredientNotFoundError):
        await c.create()(RESTAURANT, ACTOR, makro.id or 0, [LineData(999, D("1"), D("1"))])
    with pytest.raises(InvalidPurchaseOrderError):
        await c.create()(RESTAURANT, ACTOR, makro.id or 0, [])

    assert inactive.value.name == "Retirado"
    assert c.purchase_orders.rows == {}
    assert c.activity.entries == []


async def test_editing_a_draft_replaces_lines_and_note_with_a_lock() -> None:
    c = PurchasingHarness()
    makro, arroz, chicken = await c.seed()
    purchase_order = await c.create()(
        RESTAURANT, ACTOR, makro.id or 0, [LineData(arroz.id or 0, D("1"), D("1"))]
    )
    edit = EditPurchaseOrder(c.purchase_orders, c.warehouse.ingredients)

    edited_order = await edit(
        RESTAURANT, purchase_order.id or 0, [LineData(chicken.id or 0, D("3"), D("9"))], "Urgente"
    )

    assert [(ln.ingredient_id, ln.quantity) for ln in edited_order.lines] == [
        (chicken.id, D("3.000"))
    ]
    assert edited_order.notes == "Urgente"
    assert c.purchase_orders.locked_ids == [purchase_order.id]
    with pytest.raises(PurchaseOrderNotFoundError) as foreign:
        await edit(
            OTHER_RESTAURANT,
            purchase_order.id or 0,
            [LineData(chicken.id or 0, D("3"), D("9"))],
            None,
        )
    assert foreign.value.order_id == purchase_order.id


async def test_sending_and_cancelling_an_order_is_logged() -> None:
    c = PurchasingHarness()
    makro, arroz, _ = await c.seed()
    line = [LineData(arroz.id or 0, D("1"), D("1"))]
    first_order = await c.create()(RESTAURANT, ACTOR, makro.id or 0, line)
    second_order = await c.create()(RESTAURANT, ACTOR, makro.id or 0, line)
    change_status = ChangePurchaseOrderStatus(c.purchase_orders, c.activity)

    sent = await change_status(RESTAURANT, ACTOR, first_order.id or 0, "send")
    cancelled = await change_status(RESTAURANT, ACTOR, second_order.id or 0, "cancel")

    assert (sent.status, cancelled.status) == (
        PurchaseOrderStatus.SENT,
        PurchaseOrderStatus.CANCELLED,
    )
    assert sent.sent_at is not None
    assert cancelled.cancelled_at is not None
    assert c.purchase_orders.rows[first_order.id or 0].status is PurchaseOrderStatus.SENT
    assert c.purchase_orders.locked_ids == [first_order.id, second_order.id]
    assert c.activity.entries[-2:] == [
        (RESTAURANT, ACTOR, ActivityKind.PURCHASE_ORDER_SENT, "OC 1"),
        (RESTAURANT, ACTOR, ActivityKind.PURCHASE_ORDER_CANCELLED, "OC 2"),
    ]
    with pytest.raises(PurchaseOrderNotFoundError):
        await change_status(OTHER_RESTAURANT, ACTOR, first_order.id or 0, "send")


async def test_receiving_an_order_registers_purchases_at_real_cost() -> None:
    c = PurchasingHarness()
    makro, arroz, chicken = await c.seed()
    purchase_order = await c.create()(
        RESTAURANT,
        ACTOR,
        makro.id or 0,
        [LineData(arroz.id or 0, D("10"), D("4")), LineData(chicken.id or 0, D("2"), D("10"))],
    )
    arroz_line_id = purchase_order.lines[0].id or 0

    received = await c.receive()(
        RESTAURANT, ACTOR, purchase_order.id or 0, [Receipt(arroz_line_id, D("12"), D("5"))]
    )

    assert received.status is PurchaseOrderStatus.RECEIVED
    assert c.purchase_orders.locked_ids == [purchase_order.id]
    (movement,) = c.warehouse.ledger.movements
    assert (movement.ingredient_id, movement.quantity, movement.unit_cost) == (
        arroz.id,
        D("12.000"),
        D("5.000000"),
    )
    assert (movement.kind, movement.created_by, movement.reason) == (
        MovementKind.PURCHASE,
        ACTOR,
        "OC 1 · Makro",
    )
    assert c.warehouse.ingredients.rows[arroz.id or 0].unit_cost == D("5.000000")
    assert c.activity.entries[-1] == (
        RESTAURANT,
        ACTOR,
        ActivityKind.PURCHASE_ORDER_RECEIVED,
        "OC 1 de Makro (S/ 60.00)",
    )


async def test_foreign_order_cannot_be_received_nor_twice() -> None:
    c = PurchasingHarness()
    makro, arroz, _ = await c.seed()
    purchase_order = await c.create()(
        RESTAURANT, ACTOR, makro.id or 0, [LineData(arroz.id or 0, D("1"), D("1"))]
    )
    await c.receive()(RESTAURANT, ACTOR, purchase_order.id or 0, [])

    with pytest.raises(PurchaseOrderNotFoundError):
        await c.receive()(OTHER_RESTAURANT, ACTOR, purchase_order.id or 0, [])
    with pytest.raises(InvalidPurchaseOrderError):
        await c.receive()(RESTAURANT, ACTOR, purchase_order.id or 0, [])
    assert c.warehouse.ledger.movements == []


async def test_order_list_passes_the_query_through_unchanged() -> None:
    purchase_orders = InMemoryPurchaseOrders()
    query = PurchaseOrderQuery(restaurant_id=RESTAURANT)

    page = await ListPurchaseOrders(purchase_orders)(query)

    assert (page.items, page.total) == ([], 0)
    assert purchase_orders.queries == [query]


async def test_suggestions_use_a_week_of_usage() -> None:
    usage = InMemoryUsage(
        IngredientUsage(1, stock=D("10"), min_stock=D("5"), unit_cost=D("2"), used=D("70")),
        IngredientUsage(2, stock=D("500"), min_stock=D("5"), unit_cost=D("2"), used=D("7")),
    )
    before = datetime.now(UTC)

    suggestions = await SuggestPurchases(usage)(RESTAURANT)

    # 70 en 7 días son 10 por día: con 10 en stock queda un día. Se pide una semana.
    assert [(s.ingredient_id, s.average_daily_use, s.quantity) for s in suggestions] == [
        (1, D("10.000"), D("60.000"))
    ]
    (called_since,) = usage.since_calls
    assert (
        before - timedelta(days=7, seconds=5)
        <= called_since
        <= datetime.now(UTC) - timedelta(days=7)
    )


# -- Recetas ------------------------------------------------------------------------------


LOMO = Dish(id=7, name="Lomo saltado", price=D("28.00"), is_active=True)
CHICHA = Dish(id=8, name="Chicha", price=D("5.00"), is_active=True)
FREE = Dish(id=9, name="Agua", price=D("0.00"), is_active=True)


async def test_saving_a_recipe_validates_ingredients_and_logs_it() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient("Arroz", unit_cost=D("0.004"))
    meat = await a.ingredient("Carne", unit_cost=D("0.03"))
    recipes = InMemoryRecipes()
    save = ReplaceRecipe(recipes, a.ingredients, InMemoryMenu(LOMO), a.activity)

    view = await save(
        ReplaceRecipeCommand(
            RESTAURANT,
            ACTOR,
            LOMO.id,
            [RecipeLine(arroz.id or 0, D("150")), RecipeLine(meat.id or 0, D("200"))],
        )
    )

    assert [(v.ingredient.name, v.cost) for v in view.lines] == [
        ("Arroz", D("0.600000")),
        ("Carne", D("6.000000")),
    ]
    assert view.cost.cost == D("6.6")
    assert view.cost.rounded_cost == D("6.60")
    assert view.cost.margin == D("21.40")
    assert view.cost.margin_percent == D("76.4")
    assert len(recipes.rows[(RESTAURANT, LOMO.id)]) == 2
    assert a.activity.entries == [
        (RESTAURANT, ACTOR, ActivityKind.RECIPE_UPDATED, "Lomo saltado: 2 insumos")
    ]


async def test_recipe_with_foreign_retired_or_repeated_ingredient_is_not_saved() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient("Arroz")
    retired = await a.ingredient("Retirado", is_active=False)
    foreign = await a.ingredients.add(Ingredient(OTHER_RESTAURANT, "Ajeno", Unit.GRAM))
    recipes = InMemoryRecipes()
    save = ReplaceRecipe(recipes, a.ingredients, InMemoryMenu(LOMO), a.activity)

    with pytest.raises(IngredientNotFoundError) as not_found:
        await save(
            ReplaceRecipeCommand(RESTAURANT, ACTOR, LOMO.id, [RecipeLine(foreign.id or 0, D("1"))])
        )
    with pytest.raises(IngredientInactiveError):
        await save(
            ReplaceRecipeCommand(RESTAURANT, ACTOR, LOMO.id, [RecipeLine(retired.id or 0, D("1"))])
        )
    with pytest.raises(InvalidRecipeError):
        await save(
            ReplaceRecipeCommand(
                RESTAURANT,
                ACTOR,
                LOMO.id,
                [RecipeLine(arroz.id or 0, D("1")), RecipeLine(arroz.id or 0, D("2"))],
            )
        )
    with pytest.raises(DishNotFoundError) as no_dish:
        await save(ReplaceRecipeCommand(RESTAURANT, ACTOR, 99, []))

    assert not_found.value.ingredient_id == foreign.id
    assert no_dish.value.menu_item_id == 99
    assert recipes.rows == {}
    assert a.activity.entries == []


async def test_empty_recipe_is_deleted_and_the_dish_has_no_cost() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient("Arroz", unit_cost=D("1"))
    recipes = InMemoryRecipes()
    recipes.rows[(RESTAURANT, LOMO.id)] = [RecipeLine(arroz.id or 0, D("1"))]

    view = await ReplaceRecipe(recipes, a.ingredients, InMemoryMenu(LOMO), a.activity)(
        ReplaceRecipeCommand(RESTAURANT, ACTOR, LOMO.id, [])
    )

    assert recipes.rows == {}
    assert view.lines == []
    assert (view.cost.cost, view.cost.has_recipe, view.cost.margin) == (None, False, None)
    assert a.activity.entries[-1][3] == "Lomo saltado: 0 insumos"


async def test_reading_a_recipe_omits_ingredients_that_no_longer_exist() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient("Arroz", unit_cost=D("1"))
    recipes = InMemoryRecipes()
    recipes.rows[(RESTAURANT, LOMO.id)] = [
        RecipeLine(arroz.id or 0, D("2")),
        RecipeLine(555, D("1")),
    ]
    read = ReadRecipe(recipes, a.ingredients, InMemoryMenu(LOMO))

    view = await read(RESTAURANT, LOMO.id)

    assert [v.ingredient.name for v in view.lines] == ["Arroz"]
    assert view.dish is LOMO
    with pytest.raises(DishNotFoundError):
        await read(RESTAURANT, 99)


async def test_cost_of_each_dish_on_the_menu() -> None:
    a = InventoryHarness()
    arroz = await a.ingredient("Arroz", unit_cost=D("0.0333"))
    recipes = InMemoryRecipes()
    recipes.rows[(RESTAURANT, LOMO.id)] = [RecipeLine(arroz.id or 0, D("100"))]
    recipes.rows[(RESTAURANT, FREE.id)] = [RecipeLine(arroz.id or 0, D("1"))]

    costs = await ListDishCosts(recipes, a.ingredients, InMemoryMenu(LOMO, CHICHA, FREE))(
        RESTAURANT
    )

    lomo, chicha, water = costs
    assert (lomo.dish, lomo.cost, lomo.rounded_cost, lomo.margin) == (
        LOMO,
        D("3.33"),
        D("3.33"),
        D("24.67"),
    )
    assert lomo.margin_percent == D("88.1")
    assert (chicha.cost, chicha.has_recipe, chicha.margin_percent) == (None, False, None)
    # Un plato de precio cero no tiene margen porcentual.
    assert water.has_recipe
    assert water.margin_percent is None


def test_cost_rounds_half_a_cent_upwards() -> None:
    dish_cost = DishCost(dish=LOMO, cost=D("3.335"))

    assert dish_cost.rounded_cost == D("3.34")
    assert dish_cost.margin == D("24.66")
    assert dish_cost.margin_percent == D("88.1")
