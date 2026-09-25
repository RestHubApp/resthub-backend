"""Casos de uso de los insumos y su stock."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.modules.inventory.domain.entities import (
    NO_STOCK,
    ZERO,
    Ingredient,
    StockLevel,
    Unit,
    validate_name,
)
from resthub.modules.inventory.domain.exceptions import IngredientNameTaken, IngredientNotFound
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.stock_ledger import StockLedger


async def find_ingredient(
    ingredients: IngredientRepository, restaurant_id: int, ingredient_id: int
) -> Ingredient:
    ingredient = await ingredients.get(restaurant_id, ingredient_id)
    if ingredient is None:
        raise IngredientNotFound(ingredient_id)
    return ingredient


async def stock_level(ledger: StockLedger, ingredient: Ingredient) -> StockLevel:
    ingredient_id = ingredient.id or 0
    stock = await ledger.stock_of(ingredient.restaurant_id, [ingredient_id])
    return StockLevel(ingredient=ingredient, stock=stock.get(ingredient_id, NO_STOCK))


async def _ensure_name_is_free(
    ingredients: IngredientRepository, restaurant_id: int, name: str, own_id: int | None = None
) -> None:
    existing = await ingredients.find_by_name(restaurant_id, name)
    if existing is not None and existing.id != own_id:
        raise IngredientNameTaken(name)


@dataclass(frozen=True, slots=True)
class ListStockQuery:
    restaurant_id: int
    include_inactive: bool = False
    # Solo los que están por debajo del mínimo (o en negativo): las alertas.
    low_only: bool = False


class ListStock:
    def __init__(self, ingredients: IngredientRepository, ledger: StockLedger) -> None:
        self._ingredients = ingredients
        self._ledger = ledger

    async def __call__(self, query: ListStockQuery) -> list[StockLevel]:
        stock = await self._ledger.stock_of(query.restaurant_id)
        levels = [
            StockLevel(ingredient=ingredient, stock=stock.get(ingredient.id or 0, NO_STOCK))
            for ingredient in await self._ingredients.list_all(query.restaurant_id)
            # Un insumo retirado no alerta: ya no se compra.
            if ingredient.is_active or (query.include_inactive and not query.low_only)
        ]
        if query.low_only:
            # Primero lo que está en negativo y después lo más lejos del mínimo:
            # el orden en que conviene salir a comprar.
            levels = sorted(
                (level for level in levels if level.is_low),
                key=lambda level: (not level.is_negative, level.stock - level.ingredient.min_stock),
            )
        return levels


class ReadIngredient:
    def __init__(self, ingredients: IngredientRepository, ledger: StockLedger) -> None:
        self._ingredients = ingredients
        self._ledger = ledger

    async def __call__(self, restaurant_id: int, ingredient_id: int) -> StockLevel:
        ingredient = await find_ingredient(self._ingredients, restaurant_id, ingredient_id)
        return await stock_level(self._ledger, ingredient)


@dataclass(frozen=True, slots=True)
class CreateIngredientCommand:
    restaurant_id: int
    actor_id: int
    name: str
    unit: Unit
    min_stock: Decimal = ZERO
    unit_cost: Decimal = ZERO


class CreateIngredient:
    """Alta de un insumo, sin stock.

    El stock inicial entra como una compra, no como un campo del alta: así
    queda en el libro con su costo, igual que cualquier otra entrada.
    """

    def __init__(
        self,
        ingredients: IngredientRepository,
        ledger: StockLedger,
        activity: ActivityRecorder,
    ) -> None:
        self._ingredients = ingredients
        self._ledger = ledger
        self._activity = activity

    async def __call__(self, command: CreateIngredientCommand) -> StockLevel:
        name = validate_name(command.name)
        await _ensure_name_is_free(self._ingredients, command.restaurant_id, name)
        created = await self._ingredients.add(
            Ingredient(
                restaurant_id=command.restaurant_id,
                name=name,
                unit=command.unit,
                min_stock=command.min_stock,
                unit_cost=command.unit_cost,
            )
        )
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.INGREDIENT_CREATED,
            f"{created.name} ({created.unit.label})",
        )
        return StockLevel(ingredient=created, stock=NO_STOCK)


@dataclass(frozen=True, slots=True)
class UpdateIngredientCommand:
    restaurant_id: int
    actor_id: int
    ingredient_id: int
    # `None` deja el campo como está. La unidad no se edita: cambiarla
    # reinterpretaría todos los movimientos ya registrados.
    name: str | None = None
    min_stock: Decimal | None = None
    unit_cost: Decimal | None = None
    is_active: bool | None = None


class UpdateIngredient:
    def __init__(
        self,
        ingredients: IngredientRepository,
        ledger: StockLedger,
        activity: ActivityRecorder,
    ) -> None:
        self._ingredients = ingredients
        self._ledger = ledger
        self._activity = activity

    async def __call__(self, command: UpdateIngredientCommand) -> StockLevel:
        ingredient = await find_ingredient(
            self._ingredients, command.restaurant_id, command.ingredient_id
        )
        if command.name is not None:
            name = validate_name(command.name)
            await _ensure_name_is_free(
                self._ingredients, command.restaurant_id, name, ingredient.id
            )
            ingredient.rename(name)
        if command.min_stock is not None:
            ingredient.set_min_stock(command.min_stock)
        if command.unit_cost is not None:
            ingredient.set_unit_cost(command.unit_cost)
        if command.is_active is not None:
            ingredient.is_active = command.is_active

        saved = await self._ingredients.save(ingredient)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.INGREDIENT_UPDATED,
            saved.name,
        )
        return await stock_level(self._ledger, saved)
