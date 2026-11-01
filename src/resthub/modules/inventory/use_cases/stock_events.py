"""Aviso en vivo de que cambió el stock.

Todo cambio de stock es un movimiento del libro, así que el aviso sale de ahí:
el libro que reciben los casos de uso de la API publica `STOCK_TOPIC` la
primera vez que se le agrega un movimiento. Un aviso por petición alcanza
(una orden de compra con diez líneas no manda diez), y sale recién si la
transacción se confirma. El consumo al servir no pasa por acá: ese ya avisa
`orders`.
"""

from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal

from resthub.core.pagination import Page
from resthub.core.realtime import STOCK_TOPIC, EventPublisher, RealtimeEvent
from resthub.modules.inventory.domain.entities import StockMovement
from resthub.modules.inventory.ports.stock_ledger import MovementQuery, StockLedgerRepository


class AnnouncingStockLedger:
    """Un `StockLedgerRepository` que avisa al local cuando se le agregan movimientos."""

    def __init__(self, ledger: StockLedgerRepository, events: EventPublisher) -> None:
        self._ledger = ledger
        self._events = events
        self._announced: set[int] = set()

    def _announce(self, movements: list[StockMovement]) -> None:
        for restaurant_id in {movement.restaurant_id for movement in movements}:
            if restaurant_id in self._announced:
                continue
            self._announced.add(restaurant_id)
            self._events.publish(
                RealtimeEvent(restaurant_id=restaurant_id, topic=STOCK_TOPIC, everyone=True)
            )

    async def add(self, movement: StockMovement) -> StockMovement:
        saved = await self._ledger.add(movement)
        self._announce([saved])
        return saved

    async def add_many(self, movements: list[StockMovement]) -> list[StockMovement]:
        saved = await self._ledger.add_many(movements)
        self._announce(saved)
        return saved

    async def stock_of(
        self, restaurant_id: int, ingredient_ids: Collection[int] | None = None
    ) -> dict[int, Decimal]:
        return await self._ledger.stock_of(restaurant_id, ingredient_ids)

    async def search(self, query: MovementQuery) -> Page[StockMovement]:
        return await self._ledger.search(query)

    async def consumed_order_items(
        self, restaurant_id: int, order_item_ids: Collection[int]
    ) -> set[int]:
        return await self._ledger.consumed_order_items(restaurant_id, order_item_ids)
