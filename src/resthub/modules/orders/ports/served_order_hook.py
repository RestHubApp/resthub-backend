"""Lo que pasa fuera de `orders` cuando un pedido se sirve.

Servir un plato gasta insumos, pero el inventario es otro módulo y `orders` no
puede importarlo. Por eso este módulo declara el aviso que emite y no sabe
quién lo escucha: la raíz de composición conecta este puerto con el caso de uso
de consumo del inventario.

El aviso lleva todos los platos del pedido, no solo los recién servidos. Quien
escucha decide qué le falta procesar; así un segundo servido, después de
agregar platos, no puede descontar dos veces lo que ya se descontó.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ServedPortion:
    order_item_id: int
    menu_item_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class ServedOrder:
    restaurant_id: int
    order_id: int
    # Quien marcó el pedido como servido; firma los movimientos que resulten.
    actor_id: int
    portions: tuple[ServedPortion, ...]


class ServedOrderHook(Protocol):
    async def order_served(self, served: ServedOrder) -> None:
        """Corre dentro de la misma transacción: si falla, el pedido no queda servido."""
        ...
