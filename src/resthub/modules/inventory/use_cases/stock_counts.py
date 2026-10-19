"""Conteos físicos: abrir, cargar lo contado, cerrar (ajusta el stock) y sus diferencias.

Abrir, cerrar y cancelar exigen `inventory.manage`, igual que cargar las
líneas; leer, `inventory.read`. La regla de la diferencia está en
`domain/stock_counts.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.pagination import Page
from resthub.modules.inventory.domain.entities import (
    NO_STOCK,
    Ingredient,
    MovementKind,
    StockMovement,
)
from resthub.modules.inventory.domain.exceptions import (
    IngredientInactiveError,
    IngredientNotFoundError,
    InvalidStockCountError,
    StockCountAlreadyOpenError,
    StockCountNotClosedError,
    StockCountNotFoundError,
)
from resthub.modules.inventory.domain.packaging import QuantityEntry
from resthub.modules.inventory.domain.stock_counts import (
    MAX_COUNT_LINES,
    CountDifference,
    DifferenceReport,
    StockCount,
    StockCountLine,
    StockCountScope,
    StockCountStatus,
    difference_report,
    line_difference,
)
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.stock_count_repository import (
    CountedLine,
    StockCountQuery,
    StockCountRepository,
)
from resthub.modules.inventory.ports.stock_ledger import StockLedgerRepository
from resthub.modules.inventory.use_cases.manage_ingredients import find_ingredient

DEFAULT_HISTORY_SIZE = 10


async def find_stock_count(
    counts: StockCountRepository, restaurant_id: int, count_id: int, *, for_update: bool = False
) -> StockCount:
    count = await counts.get(restaurant_id, count_id, for_update=for_update)
    if count is None:
        raise StockCountNotFoundError(count_id)
    return count


@dataclass(frozen=True, slots=True)
class OpenStockCountCommand:
    restaurant_id: int
    actor_id: int
    scope: StockCountScope = StockCountScope.ALL
    # Solo con `SELECTED`: qué insumos se cuentan.
    ingredient_ids: Sequence[int] = ()
    note: str = ""


class OpenStockCount:
    """Abre un conteo con la foto del stock de cada insumo a contar."""

    def __init__(
        self,
        counts: StockCountRepository,
        ingredients: IngredientRepository,
        ledger: StockLedgerRepository,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._counts = counts
        self._ingredients = ingredients
        self._ledger = ledger
        self._activity = activity
        self._clock = clock

    async def __call__(self, command: OpenStockCountCommand) -> StockCount:
        current = await self._counts.open_count(command.restaurant_id)
        if current is not None:
            raise StockCountAlreadyOpenError(current.number)
        selected = await self._selected(command)
        stock = await self._ledger.stock_of(command.restaurant_id, [i.id or 0 for i in selected])
        lines = [
            StockCountLine(
                ingredient_id=ingredient.id or 0,
                opening_stock=stock.get(ingredient.id or 0, NO_STOCK),
                expected=stock.get(ingredient.id or 0, NO_STOCK),
            )
            for ingredient in selected
        ]
        created = await self._counts.add(
            StockCount(
                restaurant_id=command.restaurant_id,
                number=await self._counts.next_number(command.restaurant_id),
                created_by=command.actor_id,
                lines=lines,
                scope=command.scope,
                note=command.note,
                created_at=self._clock.now(),
            )
        )
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.STOCK_COUNT_OPENED,
            f"{created.reason} ({len(created.lines)} insumos)",
        )
        return created

    async def _selected(self, command: OpenStockCountCommand) -> list[Ingredient]:
        if command.scope is StockCountScope.ALL:
            active = [
                i for i in await self._ingredients.list_all(command.restaurant_id) if i.is_active
            ]
            if not active:
                message = "No hay insumos activos para contar."
                raise InvalidStockCountError(message)
            return active
        ids = list(dict.fromkeys(command.ingredient_ids))
        if not ids:
            message = "Elige al menos un insumo para contar."
            raise InvalidStockCountError(message)
        if len(ids) > MAX_COUNT_LINES:
            message = f"Un conteo admite hasta {MAX_COUNT_LINES} insumos."
            raise InvalidStockCountError(message)
        found = await self._ingredients.get_many(command.restaurant_id, ids)
        for ingredient_id in ids:
            ingredient = found.get(ingredient_id)
            if ingredient is None:
                raise IngredientNotFoundError(ingredient_id)
            if not ingredient.is_active:
                raise IngredientInactiveError(ingredient.name)
        return sorted(found.values(), key=lambda i: (i.name.casefold(), i.id or 0))


@dataclass(frozen=True, slots=True)
class CountEntry:
    ingredient_id: int
    # `None` deja el insumo sin contar.
    entry: QuantityEntry | None


@dataclass(frozen=True, slots=True)
class RecordCountLinesCommand:
    restaurant_id: int
    actor_id: int
    count_id: int
    entries: Sequence[CountEntry]


class RecordCountLines:
    """Carga o corrige lo contado de varios insumos de un conteo en curso."""

    def __init__(
        self,
        counts: StockCountRepository,
        ingredients: IngredientRepository,
        ledger: StockLedgerRepository,
        clock: Clock,
    ) -> None:
        self._counts = counts
        self._ingredients = ingredients
        self._ledger = ledger
        self._clock = clock

    async def __call__(self, command: RecordCountLinesCommand) -> StockCount:
        ids = [entry.ingredient_id for entry in command.entries]
        if len(set(ids)) != len(ids):
            message = "Un insumo aparece dos veces en la carga."
            raise InvalidStockCountError(message)
        count = await find_stock_count(
            self._counts, command.restaurant_id, command.count_id, for_update=True
        )
        ingredients = await self._ingredients.get_many(command.restaurant_id, ids)
        # El stock del sistema en este momento: contra él se mide lo contado.
        stock = await self._ledger.stock_of(command.restaurant_id, ids)
        now = self._clock.now()
        for item in command.entries:
            if item.entry is None:
                count.clear(item.ingredient_id)
                continue
            ingredient = ingredients.get(item.ingredient_id)
            count.record(
                item.ingredient_id,
                item.entry.to_base(ingredient.packaging if ingredient else None),
                item.entry,
                expected=stock.get(item.ingredient_id, NO_STOCK),
                actor_id=command.actor_id,
                now=now,
            )
        return await self._counts.save(count)


class CloseStockCount:
    """Cierra el conteo: cada diferencia entra al libro como un ajuste."""

    def __init__(
        self,
        counts: StockCountRepository,
        ingredients: IngredientRepository,
        ledger: StockLedgerRepository,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._counts = counts
        self._ingredients = ingredients
        self._ledger = ledger
        self._activity = activity
        self._clock = clock

    async def __call__(self, restaurant_id: int, actor_id: int, count_id: int) -> StockCount:
        # Con bloqueo: dos cierres a la vez no ajustan el stock dos veces.
        count = await find_stock_count(self._counts, restaurant_id, count_id, for_update=True)
        ingredients = await self._ingredients.get_many(
            restaurant_id, [line.ingredient_id for line in count.lines]
        )
        costs = {ingredient_id: i.unit_cost for ingredient_id, i in ingredients.items()}
        adjusted = count.close(costs, actor_id, self._clock.now())
        saved = await self._counts.save(count)
        await self._ledger.add_many(
            [
                StockMovement(
                    restaurant_id=restaurant_id,
                    ingredient_id=line.ingredient_id,
                    kind=MovementKind.ADJUSTMENT,
                    quantity=line.difference or Decimal(0),
                    unit_cost=line.unit_cost,
                    reason=saved.reason,
                    created_by=actor_id,
                    stock_count_id=saved.id,
                )
                for line in adjusted
            ]
        )
        report = difference_report(saved)
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.STOCK_COUNT_CLOSED,
            f"{saved.reason}: {saved.counted_lines} contados, {len(adjusted)} ajustes, "
            f"diferencia S/ {report.total_value}",
        )
        return saved


class CancelStockCount:
    """Descarta un conteo en curso sin tocar el stock."""

    def __init__(
        self, counts: StockCountRepository, activity: ActivityRecorder, clock: Clock
    ) -> None:
        self._counts = counts
        self._activity = activity
        self._clock = clock

    async def __call__(self, restaurant_id: int, actor_id: int, count_id: int) -> StockCount:
        count = await find_stock_count(self._counts, restaurant_id, count_id, for_update=True)
        count.cancel(actor_id, self._clock.now())
        saved = await self._counts.save(count)
        await self._activity.record(
            restaurant_id, actor_id, ActivityKind.STOCK_COUNT_CANCELLED, saved.reason
        )
        return saved


class ListStockCounts:
    def __init__(self, counts: StockCountRepository) -> None:
        self._counts = counts

    async def __call__(self, query: StockCountQuery) -> Page[StockCount]:
        return await self._counts.search(query)


class ReadCountDifferences:
    """El reporte de un conteo cerrado: esperado, contado y lo que vale la diferencia."""

    def __init__(self, counts: StockCountRepository) -> None:
        self._counts = counts

    async def __call__(
        self, restaurant_id: int, count_id: int
    ) -> tuple[StockCount, DifferenceReport]:
        count = await find_stock_count(self._counts, restaurant_id, count_id)
        if count.status is not StockCountStatus.CLOSED:
            raise StockCountNotClosedError(count.number)
        return count, difference_report(count)


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    counted: CountedLine
    difference: CountDifference


class IngredientCountHistory:
    """Las últimas diferencias de un insumo en conteos cerrados, para ver la tendencia."""

    def __init__(self, counts: StockCountRepository, ingredients: IngredientRepository) -> None:
        self._counts = counts
        self._ingredients = ingredients

    async def __call__(
        self, restaurant_id: int, ingredient_id: int, limit: int = DEFAULT_HISTORY_SIZE
    ) -> tuple[Ingredient, list[HistoryEntry]]:
        ingredient = await find_ingredient(self._ingredients, restaurant_id, ingredient_id)
        entries = [
            HistoryEntry(counted=counted, difference=difference)
            for counted in await self._counts.counted_lines(restaurant_id, ingredient_id, limit)
            if (difference := line_difference(counted.line)) is not None
        ]
        return ingredient, entries
