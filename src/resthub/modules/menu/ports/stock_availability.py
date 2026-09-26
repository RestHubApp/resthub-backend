"""Puerto hacia el stock del inventario: qué platos no alcanzan para una porción."""

from __future__ import annotations

from typing import Protocol


class StockAvailability(Protocol):
    async def out_of_stock(self, restaurant_id: int) -> frozenset[int]:
        """Los platos cuya receta pide de algún insumo más de lo que hay.

        Un plato sin receta nunca se agota por stock: no hay cómo saberlo.
        """
        ...
