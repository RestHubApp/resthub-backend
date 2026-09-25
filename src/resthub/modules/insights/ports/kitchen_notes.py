"""Puerto de lectura de las notas que escribe el mesero.

Las tablas son de `orders`; este módulo solo las lee.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from resthub.modules.insights.domain.decisions import KitchenNote


class KitchenNotesDirectory(Protocol):
    async def active_notes(self, restaurant_id: int) -> list[KitchenNote]:
        """Las notas no vacías de los pedidos en curso: platos y pedido entero."""
        ...

    async def notes_for_orders(
        self, restaurant_id: int, order_ids: Collection[int]
    ) -> list[KitchenNote]:
        """Las notas no vacías de esos pedidos; los de otro restaurante no aparecen."""
        ...
