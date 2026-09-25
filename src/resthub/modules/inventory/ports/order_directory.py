"""Lector hacia los pedidos que posee `orders`.

Un consumo apunta a un pedido por su identificador, pero en el salón el pedido
se nombra por su número del día. `inventory` no puede importar `orders`, así
que la pregunta se declara acá y un adaptador la responde leyendo la tabla
ajena. Se lee, nunca se escribe.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol


class OrderDirectory(Protocol):
    async def numbers(self, restaurant_id: int, order_ids: Collection[int]) -> dict[int, int]:
        """El número del día de cada pedido, por su identificador.

        Un pedido que no es del restaurante no aparece.
        """
        ...
