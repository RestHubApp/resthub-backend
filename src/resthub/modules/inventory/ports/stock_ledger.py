"""Puerto del libro de movimientos de stock.

Solo se agrega y se consulta: no hay forma de editar ni de borrar un movimiento.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.modules.inventory.domain.entities import MovementKind, StockMovement


@dataclass(frozen=True, slots=True)
class MovementQuery:
    # Obligatorio y primero: una búsqueda sin restaurante no se puede escribir.
    restaurant_id: int
    ingredient_id: int | None = None
    kinds: frozenset[MovementKind] | None = None
    order_id: int | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class StockLedger(Protocol):
    async def add(self, movement: StockMovement) -> StockMovement: ...

    async def add_many(self, movements: list[StockMovement]) -> list[StockMovement]: ...

    async def stock_of(
        self, restaurant_id: int, ingredient_ids: Collection[int] | None = None
    ) -> dict[int, Decimal]:
        """Suma de movimientos por insumo. Un insumo sin movimientos no aparece."""
        ...

    async def search(self, query: MovementQuery) -> Page[StockMovement]: ...

    async def consumed_order_items(
        self, restaurant_id: int, order_item_ids: Collection[int]
    ) -> set[int]:
        """De esos ítems, los que ya descontaron insumos.

        Se pregunta por ítem y no por pedido: al unir dos mesas los ítems
        cambian de pedido, y uno ya servido no se descuenta otra vez.
        """
        ...
