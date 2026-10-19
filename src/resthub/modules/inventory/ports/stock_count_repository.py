"""Puerto de persistencia de los conteos físicos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.modules.inventory.domain.stock_counts import (
    StockCount,
    StockCountLine,
    StockCountStatus,
)


@dataclass(frozen=True, slots=True)
class StockCountQuery:
    restaurant_id: int
    status: StockCountStatus | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


@dataclass(frozen=True, slots=True)
class CountedLine:
    """Una línea de un conteo cerrado, con el número del conteo y cuándo se cerró."""

    count_id: int
    count_number: int
    closed_at: datetime
    line: StockCountLine


class StockCountRepository(Protocol):
    async def add(self, count: StockCount) -> StockCount:
        """Lanza `StockCountAlreadyOpenError` si el local ya tiene uno abierto."""
        ...

    async def get(
        self, restaurant_id: int, count_id: int, *, for_update: bool = False
    ) -> StockCount | None: ...

    async def open_count(self, restaurant_id: int) -> StockCount | None:
        """El conteo en curso del local, si hay uno."""
        ...

    async def save(self, count: StockCount) -> StockCount: ...

    async def search(self, query: StockCountQuery) -> Page[StockCount]:
        """Del más reciente al más antiguo."""
        ...

    async def next_number(self, restaurant_id: int) -> int: ...

    async def counted_lines(
        self, restaurant_id: int, ingredient_id: int, limit: int
    ) -> list[CountedLine]:
        """Las últimas líneas contadas de un insumo en conteos cerrados, de la más nueva."""
        ...
