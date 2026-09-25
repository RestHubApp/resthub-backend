"""Lo que pasa fuera de `orders` cuando la cocina recibe platos.

Un pedido llega a la cocina al enviarlo, y también cuando se le agregan platos
estando ya en cocina, listo o servido: esos van directo a preparar. En ese
momento vale la pena mirar las notas ("alérgico al maní") antes de que alguien
cocine, pero quién las mira es otro módulo. Este declara el aviso y no sabe
quién lo escucha; la raíz de composición lo conecta.

A diferencia del aviso de pedido servido, este no es parte de la transacción:
quien escucha no puede frenar ni demorar el envío. Si su trabajo tarda, lo hace
después de responder.

El aviso lleva todos los platos del pedido con su nota. Quien escucha decide qué
le falta procesar; así un segundo aviso no repite lo ya hecho.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class KitchenItem:
    order_item_id: int
    menu_item_id: int
    name: str
    notes: str


@dataclass(frozen=True, slots=True)
class SentOrder:
    restaurant_id: int
    order_id: int
    # La nota general del pedido ("mesa con un niño alérgico").
    notes: str
    items: tuple[KitchenItem, ...]


class SentToKitchenHook(Protocol):
    def order_sent(self, sent: SentOrder) -> None:
        """Toma nota y vuelve enseguida: no puede demorar ni tumbar el envío."""
        ...
