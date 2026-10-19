"""Qué platos de un pedido ya descontaron insumos del inventario.

Al anular un plato servido, lo gastado no vuelve al almacén: la comida se
preparó. Pero el encargado tiene que saberlo, y quién descontó es el
inventario, otro módulo. Este puerto solo pregunta; el adaptador lee los
movimientos de consumo sin importar el módulo dueño.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol


class ConsumedItemsReader(Protocol):
    async def consumed(self, restaurant_id: int, order_item_ids: Collection[int]) -> set[int]:
        """De esos ítems, los que ya tienen un consumo de insumos registrado."""
        ...
