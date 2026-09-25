"""Lector hacia los platos que posee `menu`.

La receta es de un plato, y su costo se compara con el precio. `inventory` no
puede importar `menu`, así que la pregunta se declara acá y un adaptador la
responde leyendo la tabla ajena. Se lee, nunca se escribe.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Dish:
    id: int
    name: str
    price: Decimal
    is_active: bool


class DishDirectory(Protocol):
    async def get(self, restaurant_id: int, menu_item_id: int) -> Dish | None: ...

    async def list_all(self, restaurant_id: int) -> list[Dish]:
        """Todos los platos del local, en el orden de la carta."""
        ...
