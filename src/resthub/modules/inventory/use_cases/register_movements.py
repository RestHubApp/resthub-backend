"""Casos de uso del libro de stock: compras, mermas y ajustes a mano.

Los consumos no se registran a mano: salen solos al servir un pedido (ver
`consume_served_order`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.modules.inventory.domain.entities import (
    Ingredient,
    MovementKind,
    StockLevel,
    StockMovement,
)
from resthub.modules.inventory.domain.exceptions import (
    IngredientInactiveError,
    InvalidMovementError,
    InvalidQuantityEntryError,
)
from resthub.modules.inventory.domain.packaging import EntryMode, QuantityEntry
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.order_directory import OrderDirectory
from resthub.modules.inventory.ports.stock_ledger import MovementQuery, StockLedgerRepository
from resthub.modules.inventory.use_cases.manage_ingredients import find_ingredient, stock_level


@dataclass(frozen=True, slots=True)
class StockChange:
    """El movimiento registrado y cómo quedó el insumo."""

    movement: StockMovement
    level: StockLevel


def _amount(ingredient: Ingredient, quantity: Decimal) -> str:
    return f"{quantity.normalize():f} {ingredient.unit.value} de {ingredient.name}"


class _Recorder:
    def __init__(
        self,
        ingredients: IngredientRepository,
        ledger: StockLedgerRepository,
        activity: ActivityRecorder,
    ) -> None:
        self._ingredients = ingredients
        self._ledger = ledger
        self._activity = activity

    async def _active_ingredient(self, restaurant_id: int, ingredient_id: int) -> Ingredient:
        ingredient = await find_ingredient(self._ingredients, restaurant_id, ingredient_id)
        if not ingredient.is_active:
            raise IngredientInactiveError(ingredient.name)
        return ingredient


@dataclass(frozen=True, slots=True)
class RegisterPurchaseCommand:
    restaurant_id: int
    actor_id: int
    ingredient_id: int
    # En la unidad del insumo; con `entry`, se calcula de ella y esto se ignora.
    quantity: Decimal
    unit_cost: Decimal
    reason: str = ""
    # Cómo se ingresó (en envases, por ejemplo); queda anotado en el movimiento.
    entry: QuantityEntry | None = None


def _purchase_quantity(command: RegisterPurchaseCommand, ingredient: Ingredient) -> Decimal:
    if command.entry is None:
        return command.quantity
    if command.entry.mode is EntryMode.WEIGHT:
        message = "Una compra se ingresa en la unidad del insumo o en envases, no por peso."
        raise InvalidQuantityEntryError(message)
    return command.entry.to_base(ingredient.packaging)


class RegisterPurchase(_Recorder):
    """Entra mercadería. Actualiza el costo del insumo por promedio ponderado."""

    async def __call__(self, command: RegisterPurchaseCommand) -> StockChange:
        ingredient = await self._active_ingredient(command.restaurant_id, command.ingredient_id)
        movement = StockMovement(
            restaurant_id=command.restaurant_id,
            ingredient_id=command.ingredient_id,
            kind=MovementKind.PURCHASE,
            quantity=_purchase_quantity(command, ingredient),
            unit_cost=command.unit_cost,
            reason=command.reason,
            created_by=command.actor_id,
            entry_mode=command.entry.mode if command.entry else None,
            entry_quantity=command.entry.quantity if command.entry else None,
        )
        before = await stock_level(self._ledger, ingredient)
        # El costo se pondera con el stock de antes de esta compra, así que se
        # calcula antes de anotarla en el libro.
        ingredient.absorb_purchase(before.stock, movement.quantity, command.unit_cost)
        await self._ingredients.save(ingredient)
        saved = await self._ledger.add(movement)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.STOCK_PURCHASE,
            _amount(ingredient, saved.quantity),
        )
        return StockChange(movement=saved, level=await stock_level(self._ledger, ingredient))


@dataclass(frozen=True, slots=True)
class RegisterWasteCommand:
    restaurant_id: int
    actor_id: int
    ingredient_id: int
    # Lo que se perdió, en positivo; el libro lo guarda restando.
    quantity: Decimal
    # Texto libre: "se venció", "se cayó la olla". Más adelante lo clasifica
    # la IA para el reporte de causas de merma.
    reason: str


class RegisterWaste(_Recorder):
    async def __call__(self, command: RegisterWasteCommand) -> StockChange:
        ingredient = await find_ingredient(
            self._ingredients, command.restaurant_id, command.ingredient_id
        )
        if command.quantity <= 0:
            message = "Indica cuánto se perdió, en positivo."
            raise InvalidMovementError(message)
        saved = await self._ledger.add(
            StockMovement(
                restaurant_id=command.restaurant_id,
                ingredient_id=command.ingredient_id,
                kind=MovementKind.WASTE,
                quantity=-command.quantity,
                # La merma se valoriza al costo vigente, para el reporte de
                # cuánto dinero se fue a la basura.
                unit_cost=ingredient.unit_cost,
                reason=command.reason,
                created_by=command.actor_id,
            )
        )
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.STOCK_WASTE,
            f"{_amount(ingredient, command.quantity)}: {saved.reason}",
        )
        return StockChange(movement=saved, level=await stock_level(self._ledger, ingredient))


@dataclass(frozen=True, slots=True)
class RegisterAdjustmentCommand:
    restaurant_id: int
    actor_id: int
    ingredient_id: int
    reason: str
    # Uno de los dos: la diferencia con signo, o lo que se contó en el
    # almacén (y la diferencia la calcula el sistema).
    quantity: Decimal | None = None
    counted_stock: Decimal | None = None


class RegisterAdjustment(_Recorder):
    """Corrige el stock sin borrar nada: agrega la diferencia al libro."""

    async def __call__(self, command: RegisterAdjustmentCommand) -> StockChange:
        if (command.quantity is None) == (command.counted_stock is None):
            message = "Indica la diferencia o el stock contado, uno de los dos."
            raise InvalidMovementError(message)
        ingredient = await find_ingredient(
            self._ingredients, command.restaurant_id, command.ingredient_id
        )
        if command.counted_stock is not None:
            if command.counted_stock < 0:
                message = "El stock contado no puede ser negativo."
                raise InvalidMovementError(message)
            current = await stock_level(self._ledger, ingredient)
            delta = command.counted_stock - current.stock
        else:
            delta = command.quantity or Decimal(0)

        saved = await self._ledger.add(
            StockMovement(
                restaurant_id=command.restaurant_id,
                ingredient_id=command.ingredient_id,
                kind=MovementKind.ADJUSTMENT,
                quantity=delta,
                unit_cost=ingredient.unit_cost,
                reason=command.reason,
                created_by=command.actor_id,
            )
        )
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.STOCK_ADJUSTMENT,
            f"{_amount(ingredient, saved.quantity)}: {saved.reason}",
        )
        return StockChange(movement=saved, level=await stock_level(self._ledger, ingredient))


@dataclass(frozen=True, slots=True)
class MovementEntry:
    movement: StockMovement
    ingredient: Ingredient
    # El número del día del pedido que lo consumió; `None` si no salió de uno.
    order_number: int | None = None


@dataclass(frozen=True, slots=True)
class ListMovementsQuery:
    restaurant_id: int
    ingredient_id: int | None = None
    kinds: frozenset[MovementKind] | None = None
    order_id: int | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class ListMovements:
    """El libro, del movimiento más nuevo al más viejo."""

    def __init__(
        self,
        ingredients: IngredientRepository,
        ledger: StockLedgerRepository,
        orders: OrderDirectory,
    ) -> None:
        self._ingredients = ingredients
        self._ledger = ledger
        self._orders = orders

    async def __call__(self, query: ListMovementsQuery) -> Page[MovementEntry]:
        page = await self._ledger.search(
            MovementQuery(
                restaurant_id=query.restaurant_id,
                ingredient_id=query.ingredient_id,
                kinds=query.kinds,
                order_id=query.order_id,
                limit=query.limit,
                offset=query.offset,
            )
        )
        ingredients = await self._ingredients.get_many(
            query.restaurant_id, {movement.ingredient_id for movement in page.items}
        )
        numbers = await self._orders.numbers(
            query.restaurant_id,
            {movement.order_id for movement in page.items if movement.order_id is not None},
        )
        return Page(
            items=[
                MovementEntry(
                    movement=movement,
                    ingredient=ingredients[movement.ingredient_id],
                    order_number=numbers.get(movement.order_id) if movement.order_id else None,
                )
                for movement in page.items
                if movement.ingredient_id in ingredients
            ],
            total=page.total,
        )
