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
    DishNotFound,
    IngredientInactive,
    IngredientNameTaken,
    IngredientNotFound,
    InvalidMovement,
    InvalidPurchaseOrder,
    InvalidRecipe,
    PurchaseOrderNotFound,
    SupplierNameTaken,
    SupplierNotFound,
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

LOCAL = 1
OTRO = 2
ACTOR = 5
D = Decimal


# -- Dobles -----------------------------------------------------------------------------


class Insumos:
    def __init__(self) -> None:
        self.rows: dict[int, Ingredient] = {}
        self.guardados = 0

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
        self.guardados += 1
        self.rows[ingredient.id or 0] = replace(ingredient)
        return replace(ingredient)


class Libro:
    def __init__(self) -> None:
        self.movimientos: list[StockMovement] = []
        self.consultas: list[MovementQuery] = []

    async def add(self, movement: StockMovement) -> StockMovement:
        stored = replace(movement, id=len(self.movimientos) + 1)
        self.movimientos.append(stored)
        return stored

    async def add_many(self, movements: list[StockMovement]) -> list[StockMovement]:
        return [await self.add(m) for m in movements]

    async def stock_of(
        self, restaurant_id: int, ingredient_ids: Collection[int] | None = None
    ) -> dict[int, Decimal]:
        stock: dict[int, Decimal] = {}
        for m in self.movimientos:
            if m.restaurant_id == restaurant_id and (
                ingredient_ids is None or m.ingredient_id in ingredient_ids
            ):
                stock[m.ingredient_id] = stock.get(m.ingredient_id, D("0.000")) + m.quantity
        return stock

    async def search(self, query: MovementQuery) -> Page[StockMovement]:
        self.consultas.append(query)
        items = [m for m in reversed(self.movimientos) if m.restaurant_id == query.restaurant_id]
        return Page(items=items, total=len(items))

    async def consumed_order_items(self, restaurant_id: int, ids: Collection[int]) -> set[int]:
        return set()


class Pedidos:
    def __init__(self, numeros: dict[int, int]) -> None:
        self.numeros = numeros

    async def numbers(self, restaurant_id: int, order_ids: Collection[int]) -> dict[int, int]:
        return {k: v for k, v in self.numeros.items() if k in order_ids}


class Proveedores:
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


class Ordenes:
    def __init__(self) -> None:
        self.rows: dict[int, PurchaseOrder] = {}
        self.bloqueos: list[int] = []
        self.consultas: list[PurchaseOrderQuery] = []

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
            self.bloqueos.append(order_id)
        found = self.rows.get(order_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def save(self, order: PurchaseOrder) -> PurchaseOrder:
        self.rows[order.id or 0] = order
        return order

    async def search(self, query: PurchaseOrderQuery) -> Page[PurchaseOrder]:
        self.consultas.append(query)
        items = [o for o in self.rows.values() if o.restaurant_id == query.restaurant_id]
        return Page(items=items, total=len(items))

    async def next_number(self, restaurant_id: int) -> int:
        return 1 + sum(1 for o in self.rows.values() if o.restaurant_id == restaurant_id)


class Recetas:
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


class Carta:
    def __init__(self, *platos: Dish) -> None:
        self.platos = list(platos)

    async def get(self, restaurant_id: int, menu_item_id: int) -> Dish | None:
        return next((p for p in self.platos if p.id == menu_item_id), None)

    async def list_all(self, restaurant_id: int) -> list[Dish]:
        return list(self.platos)


class Consumo:
    def __init__(self, *filas: IngredientUsage) -> None:
        self.filas = list(filas)
        self.desde: list[datetime] = []

    async def usage_since(self, restaurant_id: int, since: datetime) -> list[IngredientUsage]:
        self.desde.append(since)
        return list(self.filas)


class Almacen:
    def __init__(self) -> None:
        self.insumos = Insumos()
        self.libro = Libro()
        self.bitacora = RecordingActivity()

    async def insumo(self, nombre: str = "Arroz", **datos: object) -> Ingredient:
        campos: dict[str, object] = {"restaurant_id": LOCAL, "name": nombre, "unit": Unit.GRAM}
        campos.update(datos)
        return await self.insumos.add(Ingredient(**campos))  # type: ignore[arg-type]

    def compra(self) -> RegisterPurchase:
        return RegisterPurchase(self.insumos, self.libro, self.bitacora)


# -- Insumos ----------------------------------------------------------------------------


async def test_crear_un_insumo_lo_anota_sin_stock() -> None:
    a = Almacen()

    nivel = await CreateIngredient(a.insumos, a.libro, a.bitacora)(
        CreateIngredientCommand(LOCAL, ACTOR, "  Arroz  ", Unit.GRAM, D("5000"), D("0.0042"))
    )

    assert (nivel.ingredient.name, nivel.ingredient.min_stock, nivel.stock) == (
        "Arroz",
        D("5000.000"),
        D("0.000"),
    )
    assert nivel.ingredient.unit_cost == D("0.004200")
    assert a.bitacora.entries == [(LOCAL, ACTOR, ActivityKind.INGREDIENT_CREATED, "Arroz (gramos)")]


async def test_un_insumo_no_repite_nombre_en_el_local() -> None:
    a = Almacen()
    await a.insumo("Arroz")
    await a.insumos.add(Ingredient(OTRO, "Azúcar", Unit.GRAM))

    with pytest.raises(IngredientNameTaken) as error:
        await CreateIngredient(a.insumos, a.libro, a.bitacora)(
            CreateIngredientCommand(LOCAL, ACTOR, "ARROZ", Unit.GRAM)
        )
    otro_local = await CreateIngredient(a.insumos, a.libro, a.bitacora)(
        CreateIngredientCommand(LOCAL, ACTOR, "Azúcar", Unit.GRAM)
    )
    assert error.value.name == "ARROZ"
    assert otro_local.ingredient.restaurant_id == LOCAL


async def test_editar_un_insumo_cambia_solo_lo_enviado() -> None:
    a = Almacen()
    arroz = await a.insumo(min_stock=D("10"), unit_cost=D("0.004"))
    await a.compra()(RegisterPurchaseCommand(LOCAL, ACTOR, arroz.id or 0, D("100"), D("0.004")))
    editar = UpdateIngredient(a.insumos, a.libro, a.bitacora)

    nivel = await editar(
        UpdateIngredientCommand(LOCAL, ACTOR, arroz.id or 0, name=" Arroz  extra ")
    )
    assert (nivel.ingredient.name, nivel.ingredient.min_stock, nivel.stock) == (
        "Arroz extra",
        D("10.000"),
        D("100.000"),
    )
    nivel = await editar(
        UpdateIngredientCommand(
            LOCAL, ACTOR, arroz.id or 0, min_stock=D("20"), unit_cost=D("0.005"), is_active=False
        )
    )
    guardado = a.insumos.rows[arroz.id or 0]
    assert (guardado.name, guardado.min_stock, guardado.unit_cost, guardado.is_active) == (
        "Arroz extra",
        D("20.000"),
        D("0.005000"),
        False,
    )
    assert nivel.ingredient.is_active is False
    assert [e[2:] for e in a.bitacora.entries[-2:]] == [
        (ActivityKind.INGREDIENT_UPDATED, "Arroz extra"),
        (ActivityKind.INGREDIENT_UPDATED, "Arroz extra"),
    ]


async def test_editar_un_insumo_puede_conservar_su_nombre_pero_no_tomar_otro() -> None:
    a = Almacen()
    arroz = await a.insumo("Arroz")
    await a.insumo("Pollo")
    editar = UpdateIngredient(a.insumos, a.libro, a.bitacora)

    await editar(UpdateIngredientCommand(LOCAL, ACTOR, arroz.id or 0, name="ARROZ"))
    with pytest.raises(IngredientNameTaken):
        await editar(UpdateIngredientCommand(LOCAL, ACTOR, arroz.id or 0, name="pollo"))
    with pytest.raises(IngredientNotFound) as ajeno:
        await editar(UpdateIngredientCommand(OTRO, ACTOR, arroz.id or 0, name="X"))
    assert a.insumos.rows[arroz.id or 0].name == "ARROZ"
    assert ajeno.value.ingredient_id == arroz.id


async def test_leer_un_insumo_trae_su_stock() -> None:
    a = Almacen()
    arroz = await a.insumo()
    await a.insumo("Pollo")
    await a.compra()(RegisterPurchaseCommand(LOCAL, ACTOR, arroz.id or 0, D("7.5"), D("1")))

    nivel = await ReadIngredient(a.insumos, a.libro)(LOCAL, arroz.id or 0)
    sin_movimientos = await ReadIngredient(a.insumos, a.libro)(LOCAL, 2)

    assert nivel.stock == D("7.500")
    assert str(sin_movimientos.stock) == "0.000"


async def test_el_stock_lista_activos_y_las_alertas_ordenan_lo_mas_urgente() -> None:
    a = Almacen()
    arroz = await a.insumo("Arroz", min_stock=D("100"))
    pollo = await a.insumo("Pollo", min_stock=D("50"))
    sal = await a.insumo("Sal", min_stock=D("10"))
    retirado = await a.insumo("Retirado", min_stock=D("10"), is_active=False)
    await a.libro.add(
        StockMovement(LOCAL, arroz.id or 0, MovementKind.ADJUSTMENT, D("90"), 1, reason="x")
    )
    await a.libro.add(
        StockMovement(LOCAL, pollo.id or 0, MovementKind.ADJUSTMENT, D("-5"), 1, reason="x")
    )
    await a.libro.add(
        StockMovement(LOCAL, sal.id or 0, MovementKind.ADJUSTMENT, D("10"), 1, reason="x")
    )
    listar = ListStock(a.insumos, a.libro)

    activos = await listar(ListStockQuery(LOCAL))
    todos = await listar(ListStockQuery(LOCAL, include_inactive=True))
    alertas = await listar(ListStockQuery(LOCAL, include_inactive=True, low_only=True))

    assert [n.ingredient.name for n in activos] == ["Arroz", "Pollo", "Sal"]
    assert [n.stock for n in activos] == [D("90.000"), D("-5.000"), D("10.000")]
    assert [n.ingredient.id for n in todos][-1] == retirado.id
    # Primero lo negativo; después lo que más lejos está del mínimo. El retirado no alerta.
    assert [n.ingredient.name for n in alertas] == ["Pollo", "Arroz"]


async def test_las_alertas_sin_negativos_van_por_distancia_al_minimo() -> None:
    a = Almacen()
    cerca = await a.insumo("Cerca", min_stock=D("10"))
    lejos = await a.insumo("Lejos", min_stock=D("100"))
    await a.libro.add(
        StockMovement(LOCAL, cerca.id or 0, MovementKind.ADJUSTMENT, D("9"), 1, reason="x")
    )
    await a.libro.add(
        StockMovement(LOCAL, lejos.id or 0, MovementKind.ADJUSTMENT, D("1"), 1, reason="x")
    )

    alertas = await ListStock(a.insumos, a.libro)(ListStockQuery(LOCAL, low_only=True))

    assert [n.ingredient.name for n in alertas] == ["Lejos", "Cerca"]


# -- Libro de movimientos -------------------------------------------------------------------


async def test_una_compra_entra_al_libro_y_pondera_el_costo() -> None:
    a = Almacen()
    arroz = await a.insumo(unit_cost=D("4"), unit=Unit.UNIT)
    comprar = a.compra()
    await comprar(RegisterPurchaseCommand(LOCAL, ACTOR, arroz.id or 0, D("5"), D("4")))

    cambio = await comprar(
        RegisterPurchaseCommand(LOCAL, ACTOR, arroz.id or 0, D("20"), D("5"), reason="Makro")
    )

    assert (cambio.movement.kind, cambio.movement.quantity, cambio.movement.unit_cost) == (
        MovementKind.PURCHASE,
        D("20.000"),
        D("5.000000"),
    )
    assert (cambio.movement.reason, cambio.movement.created_by) == ("Makro", ACTOR)
    assert cambio.level.stock == D("25.000")
    assert a.insumos.rows[arroz.id or 0].unit_cost == D("4.800000")
    assert cambio.level.ingredient.unit_cost == D("4.800000")
    assert a.bitacora.entries[-1] == (LOCAL, ACTOR, ActivityKind.STOCK_PURCHASE, "20 unit de Arroz")


async def test_la_bitacora_de_una_compra_muestra_la_cantidad_sin_ceros_de_mas() -> None:
    a = Almacen()
    arroz = await a.insumo()

    await a.compra()(RegisterPurchaseCommand(LOCAL, ACTOR, arroz.id or 0, D("1.500"), D("1")))
    await a.compra()(RegisterPurchaseCommand(LOCAL, ACTOR, arroz.id or 0, D("1000"), D("1")))

    assert [e[3] for e in a.bitacora.entries] == ["1.5 g de Arroz", "1000 g de Arroz"]


async def test_no_se_compra_ni_se_ajusta_un_insumo_retirado_o_ajeno() -> None:
    a = Almacen()
    retirado = await a.insumo("Retirado", is_active=False)
    ajeno = await a.insumos.add(Ingredient(OTRO, "Ajeno", Unit.GRAM))

    with pytest.raises(IngredientInactive) as inactivo:
        await a.compra()(RegisterPurchaseCommand(LOCAL, ACTOR, retirado.id or 0, D("1"), D("1")))
    with pytest.raises(IngredientNotFound):
        await a.compra()(RegisterPurchaseCommand(LOCAL, ACTOR, ajeno.id or 0, D("1"), D("1")))
    assert inactivo.value.name == "Retirado"
    assert a.libro.movimientos == []
    assert a.insumos.guardados == 0


async def test_la_merma_resta_al_costo_vigente_y_explica_el_motivo() -> None:
    a = Almacen()
    arroz = await a.insumo(unit_cost=D("0.004"))
    await a.compra()(RegisterPurchaseCommand(LOCAL, ACTOR, arroz.id or 0, D("1000"), D("0.004")))

    cambio = await RegisterWaste(a.insumos, a.libro, a.bitacora)(
        RegisterWasteCommand(LOCAL, ACTOR, arroz.id or 0, D("250"), "  se   quemó ")
    )

    assert (cambio.movement.kind, cambio.movement.quantity) == (MovementKind.WASTE, D("-250.000"))
    assert cambio.movement.unit_cost == D("0.004000")
    assert cambio.movement.created_by == ACTOR
    assert cambio.level.stock == D("750.000")
    assert a.bitacora.entries[-1] == (
        LOCAL,
        ACTOR,
        ActivityKind.STOCK_WASTE,
        "250 g de Arroz: se quemó",
    )


@pytest.mark.parametrize("cantidad", ["0", "-1"])
async def test_la_merma_se_indica_en_positivo(cantidad: str) -> None:
    a = Almacen()
    arroz = await a.insumo()

    with pytest.raises(InvalidMovement) as error:
        await RegisterWaste(a.insumos, a.libro, a.bitacora)(
            RegisterWasteCommand(LOCAL, ACTOR, arroz.id or 0, D(cantidad), "se cayó")
        )
    assert error.value.reason == "Indica cuánto se perdió, en positivo."
    assert a.libro.movimientos == []


async def test_el_ajuste_por_diferencia_suma_con_signo() -> None:
    a = Almacen()
    arroz = await a.insumo(unit_cost=D("2"))

    cambio = await RegisterAdjustment(a.insumos, a.libro, a.bitacora)(
        RegisterAdjustmentCommand(LOCAL, ACTOR, arroz.id or 0, "conteo", quantity=D("-3"))
    )

    assert (cambio.movement.kind, cambio.movement.quantity, cambio.movement.unit_cost) == (
        MovementKind.ADJUSTMENT,
        D("-3.000"),
        D("2.000000"),
    )
    assert cambio.movement.created_by == ACTOR
    assert cambio.level.stock == D("-3.000")
    assert a.bitacora.entries[-1] == (
        LOCAL,
        ACTOR,
        ActivityKind.STOCK_ADJUSTMENT,
        "-3 g de Arroz: conteo",
    )


async def test_el_ajuste_por_conteo_calcula_la_diferencia_contra_el_libro() -> None:
    a = Almacen()
    arroz = await a.insumo()
    await a.compra()(RegisterPurchaseCommand(LOCAL, ACTOR, arroz.id or 0, D("10"), D("1")))
    ajustar = RegisterAdjustment(a.insumos, a.libro, a.bitacora)

    cambio = await ajustar(
        RegisterAdjustmentCommand(LOCAL, ACTOR, arroz.id or 0, "inventario", counted_stock=D("7.5"))
    )
    a_cero = await ajustar(
        RegisterAdjustmentCommand(LOCAL, ACTOR, arroz.id or 0, "se acabó", counted_stock=D("0"))
    )

    assert cambio.movement.quantity == D("-2.500")
    assert cambio.level.stock == D("7.500")
    assert a_cero.movement.quantity == D("-7.500")
    assert a_cero.level.stock == D("0.000")


@pytest.mark.parametrize(
    ("datos", "mensaje"),
    [
        ({}, "Indica la diferencia o el stock contado, uno de los dos."),
        (
            {"quantity": D("1"), "counted_stock": D("1")},
            "Indica la diferencia o el stock contado, uno de los dos.",
        ),
        ({"counted_stock": D("-0.001")}, "El stock contado no puede ser negativo."),
    ],
)
async def test_un_ajuste_mal_pedido_se_rechaza(datos: dict[str, Decimal], mensaje: str) -> None:
    a = Almacen()
    arroz = await a.insumo()

    with pytest.raises(InvalidMovement) as error:
        await RegisterAdjustment(a.insumos, a.libro, a.bitacora)(
            RegisterAdjustmentCommand(LOCAL, ACTOR, arroz.id or 0, "conteo", **datos)
        )
    assert error.value.reason == mensaje
    assert a.libro.movimientos == []


async def test_un_ajuste_sobre_un_insumo_ajeno_no_existe() -> None:
    a = Almacen()
    ajeno = await a.insumos.add(Ingredient(OTRO, "Ajeno", Unit.GRAM))

    with pytest.raises(IngredientNotFound):
        await RegisterAdjustment(a.insumos, a.libro, a.bitacora)(
            RegisterAdjustmentCommand(LOCAL, ACTOR, ajeno.id or 0, "conteo", quantity=D("1"))
        )
    with pytest.raises(IngredientNotFound):
        await RegisterWaste(a.insumos, a.libro, a.bitacora)(
            RegisterWasteCommand(LOCAL, ACTOR, ajeno.id or 0, D("1"), "x")
        )


async def test_el_libro_trae_insumo_y_numero_de_pedido_de_cada_movimiento() -> None:
    a = Almacen()
    arroz = await a.insumo()
    await a.libro.add(
        StockMovement(LOCAL, arroz.id or 0, MovementKind.PURCHASE, D("10"), 1, unit_cost=D("1"))
    )
    await a.libro.add(
        StockMovement(
            LOCAL, arroz.id or 0, MovementKind.CONSUMPTION, D("-1"), 1, order_id=40, order_item_id=4
        )
    )
    await a.libro.add(StockMovement(LOCAL, 999, MovementKind.ADJUSTMENT, D("1"), 1, reason="x"))

    pagina = await ListMovements(a.insumos, a.libro, Pedidos({40: 14}))(
        ListMovementsQuery(
            LOCAL,
            ingredient_id=arroz.id,
            kinds=frozenset({MovementKind.CONSUMPTION}),
            order_id=40,
            limit=5,
            offset=1,
        )
    )

    assert [(e.movement.kind, e.ingredient.name, e.order_number) for e in pagina.items] == [
        (MovementKind.CONSUMPTION, "Arroz", 14),
        (MovementKind.PURCHASE, "Arroz", None),
    ]
    assert pagina.total == 3
    assert a.libro.consultas == [
        MovementQuery(
            restaurant_id=LOCAL,
            ingredient_id=arroz.id,
            kinds=frozenset({MovementKind.CONSUMPTION}),
            order_id=40,
            limit=5,
            offset=1,
        )
    ]


# -- Proveedores y órdenes de compra -----------------------------------------------------------


async def test_alta_y_edicion_de_un_proveedor() -> None:
    proveedores, bitacora = Proveedores(), RecordingActivity()
    guardar = SaveSupplier(proveedores, bitacora)

    alta = await guardar(
        LOCAL, ACTOR, SupplierData("Makro", contact="Rosa", phone="999", notes="lunes")
    )
    editado = await guardar(
        LOCAL,
        ACTOR,
        SupplierData("Makro Surco", contact="Luis", phone="888", notes="martes", is_active=False),
        alta.id,
    )

    assert (alta.name, alta.contact, alta.phone, alta.notes, alta.is_active) == (
        "Makro",
        "Rosa",
        "999",
        "lunes",
        True,
    )
    assert editado.id == alta.id
    assert editado.created_at == alta.created_at
    guardado = proveedores.rows[alta.id or 0]
    assert (
        guardado.name,
        guardado.contact,
        guardado.phone,
        guardado.notes,
        guardado.is_active,
    ) == (
        "Makro Surco",
        "Luis",
        "888",
        "martes",
        False,
    )
    assert bitacora.entries == [
        (LOCAL, ACTOR, ActivityKind.SUPPLIER_CREATED, "Makro"),
        (LOCAL, ACTOR, ActivityKind.SUPPLIER_UPDATED, "Makro Surco"),
    ]


async def test_un_proveedor_no_repite_nombre_pero_puede_conservar_el_suyo() -> None:
    proveedores, bitacora = Proveedores(), RecordingActivity()
    guardar = SaveSupplier(proveedores, bitacora)
    makro = await guardar(LOCAL, ACTOR, SupplierData("Makro"))
    await guardar(LOCAL, ACTOR, SupplierData("Tottus"))

    await guardar(LOCAL, ACTOR, SupplierData("MAKRO"), makro.id)
    with pytest.raises(SupplierNameTaken) as repetido:
        await guardar(LOCAL, ACTOR, SupplierData("tottus"), makro.id)
    with pytest.raises(SupplierNameTaken):
        await guardar(LOCAL, ACTOR, SupplierData("Tottus"))
    with pytest.raises(SupplierNotFound) as ajeno:
        await guardar(OTRO, ACTOR, SupplierData("Makro"), makro.id)

    assert repetido.value.name == "tottus"
    assert ajeno.value.supplier_id == makro.id
    assert proveedores.rows[makro.id or 0].name == "MAKRO"
    assert len(bitacora.entries) == 3


class Compras:
    def __init__(self) -> None:
        self.almacen = Almacen()
        self.proveedores = Proveedores()
        self.ordenes = Ordenes()
        self.bitacora = self.almacen.bitacora

    async def preparar(self) -> tuple[Supplier, Ingredient, Ingredient]:
        makro = await self.proveedores.add(Supplier(LOCAL, "Makro"))
        arroz = await self.almacen.insumo("Arroz", unit_cost=D("4"), unit=Unit.UNIT)
        pollo = await self.almacen.insumo("Pollo", unit_cost=D("10"), unit=Unit.UNIT)
        return makro, arroz, pollo

    def crear(self) -> CreatePurchaseOrder:
        return CreatePurchaseOrder(
            self.ordenes, self.proveedores, self.almacen.insumos, self.bitacora
        )

    def recibir(self) -> ReceivePurchaseOrder:
        return ReceivePurchaseOrder(
            self.ordenes, self.proveedores, self.almacen.compra(), self.bitacora
        )


async def test_crear_una_orden_numera_por_local_y_la_anota() -> None:
    c = Compras()
    makro, arroz, pollo = await c.preparar()

    orden = await c.crear()(
        LOCAL,
        ACTOR,
        makro.id or 0,
        [LineData(arroz.id or 0, D("10"), D("4.5")), LineData(pollo.id or 0, D("2"), D("10"))],
        notes="Para el viernes",
    )
    segunda = await c.crear()(
        LOCAL, ACTOR, makro.id or 0, [LineData(arroz.id or 0, D("1"), D("1"))]
    )

    assert (orden.number, orden.supplier_id, orden.created_by, orden.notes) == (
        1,
        makro.id,
        ACTOR,
        "Para el viernes",
    )
    assert [(ln.ingredient_id, ln.quantity, ln.unit_cost) for ln in orden.lines] == [
        (arroz.id, D("10.000"), D("4.500000")),
        (pollo.id, D("2.000"), D("10.000000")),
    ]
    assert segunda.number == 2
    assert segunda.notes == ""
    assert c.bitacora.entries[0] == (
        LOCAL,
        ACTOR,
        ActivityKind.PURCHASE_ORDER_CREATED,
        "OC 1 a Makro (S/ 65.00)",
    )


async def test_una_orden_no_se_arma_con_proveedor_o_insumo_ajeno_o_retirado() -> None:
    c = Compras()
    makro, arroz, _ = await c.preparar()
    retirado = await c.almacen.insumo("Retirado", is_active=False)
    ajeno = await c.proveedores.add(Supplier(OTRO, "Ajeno"))
    linea = [LineData(arroz.id or 0, D("1"), D("1"))]

    with pytest.raises(SupplierNotFound):
        await c.crear()(LOCAL, ACTOR, ajeno.id or 0, linea)
    with pytest.raises(IngredientInactive) as inactivo:
        await c.crear()(LOCAL, ACTOR, makro.id or 0, [LineData(retirado.id or 0, D("1"), D("1"))])
    with pytest.raises(IngredientNotFound):
        await c.crear()(LOCAL, ACTOR, makro.id or 0, [LineData(999, D("1"), D("1"))])
    with pytest.raises(InvalidPurchaseOrder):
        await c.crear()(LOCAL, ACTOR, makro.id or 0, [])

    assert inactivo.value.name == "Retirado"
    assert c.ordenes.rows == {}
    assert c.bitacora.entries == []


async def test_editar_un_borrador_reemplaza_lineas_y_nota_con_bloqueo() -> None:
    c = Compras()
    makro, arroz, pollo = await c.preparar()
    orden = await c.crear()(LOCAL, ACTOR, makro.id or 0, [LineData(arroz.id or 0, D("1"), D("1"))])
    editar = EditPurchaseOrder(c.ordenes, c.almacen.insumos)

    editada = await editar(
        LOCAL, orden.id or 0, [LineData(pollo.id or 0, D("3"), D("9"))], "Urgente"
    )

    assert [(ln.ingredient_id, ln.quantity) for ln in editada.lines] == [(pollo.id, D("3.000"))]
    assert editada.notes == "Urgente"
    assert c.ordenes.bloqueos == [orden.id]
    with pytest.raises(PurchaseOrderNotFound) as ajena:
        await editar(OTRO, orden.id or 0, [LineData(pollo.id or 0, D("3"), D("9"))], None)
    assert ajena.value.order_id == orden.id


async def test_enviar_y_cancelar_una_orden_queda_en_la_bitacora() -> None:
    c = Compras()
    makro, arroz, _ = await c.preparar()
    linea = [LineData(arroz.id or 0, D("1"), D("1"))]
    uno = await c.crear()(LOCAL, ACTOR, makro.id or 0, linea)
    dos = await c.crear()(LOCAL, ACTOR, makro.id or 0, linea)
    cambiar = ChangePurchaseOrderStatus(c.ordenes, c.bitacora)

    enviada = await cambiar(LOCAL, ACTOR, uno.id or 0, "send")
    cancelada = await cambiar(LOCAL, ACTOR, dos.id or 0, "cancel")

    assert (enviada.status, cancelada.status) == (
        PurchaseOrderStatus.SENT,
        PurchaseOrderStatus.CANCELLED,
    )
    assert enviada.sent_at is not None and cancelada.cancelled_at is not None
    assert c.ordenes.rows[uno.id or 0].status is PurchaseOrderStatus.SENT
    assert c.ordenes.bloqueos == [uno.id, dos.id]
    assert c.bitacora.entries[-2:] == [
        (LOCAL, ACTOR, ActivityKind.PURCHASE_ORDER_SENT, "OC 1"),
        (LOCAL, ACTOR, ActivityKind.PURCHASE_ORDER_CANCELLED, "OC 2"),
    ]
    with pytest.raises(PurchaseOrderNotFound):
        await cambiar(OTRO, ACTOR, uno.id or 0, "send")


async def test_recibir_una_orden_registra_compras_con_el_costo_real() -> None:
    c = Compras()
    makro, arroz, pollo = await c.preparar()
    orden = await c.crear()(
        LOCAL,
        ACTOR,
        makro.id or 0,
        [LineData(arroz.id or 0, D("10"), D("4")), LineData(pollo.id or 0, D("2"), D("10"))],
    )
    linea_arroz = orden.lines[0].id or 0

    recibida = await c.recibir()(
        LOCAL, ACTOR, orden.id or 0, [Receipt(linea_arroz, D("12"), D("5"))]
    )

    assert recibida.status is PurchaseOrderStatus.RECEIVED
    assert c.ordenes.bloqueos == [orden.id]
    (movimiento,) = c.almacen.libro.movimientos
    assert (movimiento.ingredient_id, movimiento.quantity, movimiento.unit_cost) == (
        arroz.id,
        D("12.000"),
        D("5.000000"),
    )
    assert (movimiento.kind, movimiento.created_by, movimiento.reason) == (
        MovementKind.PURCHASE,
        ACTOR,
        "OC 1 · Makro",
    )
    assert c.almacen.insumos.rows[arroz.id or 0].unit_cost == D("5.000000")
    assert c.bitacora.entries[-1] == (
        LOCAL,
        ACTOR,
        ActivityKind.PURCHASE_ORDER_RECEIVED,
        "OC 1 de Makro (S/ 60.00)",
    )


async def test_no_se_recibe_una_orden_ajena_ni_dos_veces() -> None:
    c = Compras()
    makro, arroz, _ = await c.preparar()
    orden = await c.crear()(LOCAL, ACTOR, makro.id or 0, [LineData(arroz.id or 0, D("1"), D("1"))])
    await c.recibir()(LOCAL, ACTOR, orden.id or 0, [])

    with pytest.raises(PurchaseOrderNotFound):
        await c.recibir()(OTRO, ACTOR, orden.id or 0, [])
    with pytest.raises(InvalidPurchaseOrder):
        await c.recibir()(LOCAL, ACTOR, orden.id or 0, [])
    assert c.almacen.libro.movimientos == []


async def test_la_lista_de_ordenes_pasa_la_consulta_tal_cual() -> None:
    ordenes = Ordenes()
    consulta = PurchaseOrderQuery(restaurant_id=LOCAL)

    pagina = await ListPurchaseOrders(ordenes)(consulta)

    assert (pagina.items, pagina.total) == ([], 0)
    assert ordenes.consultas == [consulta]


async def test_las_sugerencias_usan_una_semana_de_consumo() -> None:
    consumo = Consumo(
        IngredientUsage(1, stock=D("10"), min_stock=D("5"), unit_cost=D("2"), used=D("70")),
        IngredientUsage(2, stock=D("500"), min_stock=D("5"), unit_cost=D("2"), used=D("7")),
    )
    antes = datetime.now(UTC)

    sugerencias = await SuggestPurchases(consumo)(LOCAL)

    # 70 en 7 días son 10 por día: con 10 en stock queda un día. Se pide una semana.
    assert [(s.ingredient_id, s.average_daily_use, s.quantity) for s in sugerencias] == [
        (1, D("10.000"), D("60.000"))
    ]
    (desde,) = consumo.desde
    assert antes - timedelta(days=7, seconds=5) <= desde <= datetime.now(UTC) - timedelta(days=7)


# -- Recetas ------------------------------------------------------------------------------


LOMO = Dish(id=7, name="Lomo saltado", price=D("28.00"), is_active=True)
CHICHA = Dish(id=8, name="Chicha", price=D("5.00"), is_active=True)
GRATIS = Dish(id=9, name="Agua", price=D("0.00"), is_active=True)


async def test_guardar_una_receta_valida_insumos_y_la_anota() -> None:
    a = Almacen()
    arroz = await a.insumo("Arroz", unit_cost=D("0.004"))
    carne = await a.insumo("Carne", unit_cost=D("0.03"))
    recetas = Recetas()
    guardar = ReplaceRecipe(recetas, a.insumos, Carta(LOMO), a.bitacora)

    vista = await guardar(
        ReplaceRecipeCommand(
            LOCAL,
            ACTOR,
            LOMO.id,
            [RecipeLine(arroz.id or 0, D("150")), RecipeLine(carne.id or 0, D("200"))],
        )
    )

    assert [(v.ingredient.name, v.cost) for v in vista.lines] == [
        ("Arroz", D("0.600000")),
        ("Carne", D("6.000000")),
    ]
    assert vista.cost.cost == D("6.6")
    assert vista.cost.rounded_cost == D("6.60")
    assert vista.cost.margin == D("21.40")
    assert vista.cost.margin_percent == D("76.4")
    assert len(recetas.rows[(LOCAL, LOMO.id)]) == 2
    assert a.bitacora.entries == [
        (LOCAL, ACTOR, ActivityKind.RECIPE_UPDATED, "Lomo saltado: 2 insumos")
    ]


async def test_una_receta_con_insumo_ajeno_retirado_o_repetido_no_se_guarda() -> None:
    a = Almacen()
    arroz = await a.insumo("Arroz")
    retirado = await a.insumo("Retirado", is_active=False)
    ajeno = await a.insumos.add(Ingredient(OTRO, "Ajeno", Unit.GRAM))
    recetas = Recetas()
    guardar = ReplaceRecipe(recetas, a.insumos, Carta(LOMO), a.bitacora)

    with pytest.raises(IngredientNotFound) as no_existe:
        await guardar(
            ReplaceRecipeCommand(LOCAL, ACTOR, LOMO.id, [RecipeLine(ajeno.id or 0, D("1"))])
        )
    with pytest.raises(IngredientInactive):
        await guardar(
            ReplaceRecipeCommand(LOCAL, ACTOR, LOMO.id, [RecipeLine(retirado.id or 0, D("1"))])
        )
    with pytest.raises(InvalidRecipe):
        await guardar(
            ReplaceRecipeCommand(
                LOCAL,
                ACTOR,
                LOMO.id,
                [RecipeLine(arroz.id or 0, D("1")), RecipeLine(arroz.id or 0, D("2"))],
            )
        )
    with pytest.raises(DishNotFound) as sin_plato:
        await guardar(ReplaceRecipeCommand(LOCAL, ACTOR, 99, []))

    assert no_existe.value.ingredient_id == ajeno.id
    assert sin_plato.value.menu_item_id == 99
    assert recetas.rows == {}
    assert a.bitacora.entries == []


async def test_una_receta_vacia_se_borra_y_el_plato_queda_sin_costo() -> None:
    a = Almacen()
    arroz = await a.insumo("Arroz", unit_cost=D("1"))
    recetas = Recetas()
    recetas.rows[(LOCAL, LOMO.id)] = [RecipeLine(arroz.id or 0, D("1"))]

    vista = await ReplaceRecipe(recetas, a.insumos, Carta(LOMO), a.bitacora)(
        ReplaceRecipeCommand(LOCAL, ACTOR, LOMO.id, [])
    )

    assert recetas.rows == {}
    assert vista.lines == []
    assert (vista.cost.cost, vista.cost.has_recipe, vista.cost.margin) == (None, False, None)
    assert a.bitacora.entries[-1][3] == "Lomo saltado: 0 insumos"


async def test_leer_una_receta_omite_insumos_que_ya_no_estan() -> None:
    a = Almacen()
    arroz = await a.insumo("Arroz", unit_cost=D("1"))
    recetas = Recetas()
    recetas.rows[(LOCAL, LOMO.id)] = [RecipeLine(arroz.id or 0, D("2")), RecipeLine(555, D("1"))]
    leer = ReadRecipe(recetas, a.insumos, Carta(LOMO))

    vista = await leer(LOCAL, LOMO.id)

    assert [v.ingredient.name for v in vista.lines] == ["Arroz"]
    assert vista.dish is LOMO
    with pytest.raises(DishNotFound):
        await leer(LOCAL, 99)


async def test_el_costo_de_cada_plato_de_la_carta() -> None:
    a = Almacen()
    arroz = await a.insumo("Arroz", unit_cost=D("0.0333"))
    recetas = Recetas()
    recetas.rows[(LOCAL, LOMO.id)] = [RecipeLine(arroz.id or 0, D("100"))]
    recetas.rows[(LOCAL, GRATIS.id)] = [RecipeLine(arroz.id or 0, D("1"))]

    costos = await ListDishCosts(recetas, a.insumos, Carta(LOMO, CHICHA, GRATIS))(LOCAL)

    lomo, chicha, agua = costos
    assert (lomo.dish, lomo.cost, lomo.rounded_cost, lomo.margin) == (
        LOMO,
        D("3.33"),
        D("3.33"),
        D("24.67"),
    )
    assert lomo.margin_percent == D("88.1")
    assert (chicha.cost, chicha.has_recipe, chicha.margin_percent) == (None, False, None)
    # Un plato de precio cero no tiene margen porcentual.
    assert agua.has_recipe and agua.margin_percent is None


def test_el_costo_redondea_al_medio_centimo_hacia_arriba() -> None:
    costo = DishCost(dish=LOMO, cost=D("3.335"))

    assert costo.rounded_cost == D("3.34")
    assert costo.margin == D("24.66")
    assert costo.margin_percent == D("88.1")
