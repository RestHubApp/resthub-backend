"""Puerto de lectura de la zona horaria del restaurante.

Los reportes cuentan por día del local, y la zona la posee `restaurants`.
"""

from __future__ import annotations

from typing import Protocol


class RestaurantCalendar(Protocol):
    async def timezone(self, restaurant_id: int) -> str: ...
