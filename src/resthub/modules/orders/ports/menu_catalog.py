"""Lector hacia los platos que posee `menu`.

Un pedido necesita saber si un plato existe en el local, si se puede pedir hoy
y cuánto cuesta. `orders` no puede importar `menu`, así que la pregunta se
declara acá y un adaptador la responde leyendo la tabla ajena. Se lee, nunca se
escribe.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from resthub.modules.orders.domain.modifiers import DishOptionGroup


@dataclass(frozen=True, slots=True)
class OrderableDish:
    id: int
    name: str
    price: Decimal
    is_active: bool
    is_available: bool
    modifier_groups: tuple[DishOptionGroup, ...] = ()
    # Su receta pide más de algún insumo del que hay: no alcanza para una porción.
    out_of_stock: bool = False

    @property
    def can_be_ordered(self) -> bool:
        return self.is_active and self.is_available and not self.out_of_stock


class MenuCatalog(Protocol):
    async def get_dishes(
        self, restaurant_id: int, dish_ids: Collection[int]
    ) -> dict[int, OrderableDish]:
        """Los platos pedidos que existen en ese restaurante; los ajenos no aparecen."""
        ...
