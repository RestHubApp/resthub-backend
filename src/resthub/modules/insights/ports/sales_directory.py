"""Puerto de lectura de pedidos, platos y personal para los reportes de venta.

Las tablas son de `orders`, `menu`, `inventory` y `accounts`; este módulo solo
las lee, por SQL, desde su adaptador `directories`.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from resthub.modules.insights.domain.period import DateRange
from resthub.modules.insights.domain.sales import CatalogDish, OrderFact, PaymentFact, SoldDish


class SalesDirectory(Protocol):
    async def closed_orders(self, restaurant_id: int, period: DateRange) -> list[OrderFact]:
        """Pedidos pagados y cancelados abiertos en esos días del restaurante."""
        ...

    async def payments(self, restaurant_id: int, period: DateRange) -> list[PaymentFact]:
        """Los pagos de los pedidos pagados abiertos en esos días del restaurante."""
        ...

    async def sold_dishes(self, restaurant_id: int, period: DateRange) -> list[SoldDish]:
        """Cantidad e ingresos por plato, solo de pedidos pagados.

        Una cortesía cuenta como porción servida pero no como ingreso.
        """
        ...

    async def dish_catalog(self, restaurant_id: int) -> list[CatalogDish]:
        """Todos los platos, con el costo de una porción según su receta."""
        ...

    async def staff_names(
        self, restaurant_id: int, user_ids: Collection[int]
    ) -> dict[int, str]: ...
