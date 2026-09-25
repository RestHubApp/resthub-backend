"""Lector hacia la zona horaria que posee `restaurants`.

El correlativo diario depende del día del restaurante, así que hay que saber
en qué zona está. Se lee por SQL desde un adaptador, como cualquier dato de otro
módulo.
"""

from __future__ import annotations

from typing import Protocol


class RestaurantClock(Protocol):
    async def timezone_for_numbering(self, restaurant_id: int) -> str:
        """La zona del restaurante, reservando la numeración de sus pedidos.

        Además de leer, serializa la numeración: dos pedidos abiertos a la vez
        en el mismo local esperan su turno para calcular el número siguiente y
        para comprobar si la mesa sigue libre. En un local chico la espera es
        imperceptible, y evita dos pedidos con el mismo número o en la misma
        mesa.
        """
        ...
