"""Puerto de lectura del almacén: stock, consumos, compras y mermas.

Las tablas son de `inventory`; este módulo solo las lee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from resthub.modules.insights.domain.stock import IngredientFlow, StockFact, WasteFact


@dataclass(frozen=True, slots=True)
class FlowWindows:
    short_since: datetime
    long_since: datetime


class StockDirectory(Protocol):
    async def stock_levels(self, restaurant_id: int) -> list[StockFact]:
        """Los insumos activos con su stock."""
        ...

    async def flows(self, restaurant_id: int, windows: FlowWindows) -> dict[int, IngredientFlow]:
        """Lo que salió de cada insumo en cada ventana.

        También su primer movimiento y su última compra.
        """
        ...

    async def wastes(
        self,
        restaurant_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[WasteFact]:
        """Las mermas del rango, de la más nueva a la más vieja, sin causa asignada."""
        ...
