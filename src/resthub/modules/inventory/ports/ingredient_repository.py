"""Puerto de persistencia de los insumos."""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from resthub.modules.inventory.domain.entities import Ingredient


class IngredientRepository(Protocol):
    async def add(self, ingredient: Ingredient) -> Ingredient: ...

    async def get(self, restaurant_id: int, ingredient_id: int) -> Ingredient | None: ...

    async def get_many(
        self, restaurant_id: int, ingredient_ids: Collection[int]
    ) -> dict[int, Ingredient]:
        """Los pedidos que son de ese restaurante; los ajenos no aparecen."""
        ...

    async def find_by_name(self, restaurant_id: int, name: str) -> Ingredient | None:
        """Sin distinguir mayúsculas."""
        ...

    async def list_all(self, restaurant_id: int) -> list[Ingredient]:
        """Todos, activos o no, por nombre."""
        ...

    async def save(self, ingredient: Ingredient) -> Ingredient: ...
