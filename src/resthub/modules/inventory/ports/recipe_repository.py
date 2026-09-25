"""Puerto de persistencia de las recetas.

Una receta no es una entidad con vida propia: es el conjunto de líneas de un
plato. Por eso se reemplaza entera, que es como la edita el encargado.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from resthub.modules.inventory.domain.entities import RecipeLine


class RecipeRepository(Protocol):
    async def lines_for(self, restaurant_id: int, menu_item_id: int) -> list[RecipeLine]: ...

    async def lines_for_many(
        self, restaurant_id: int, menu_item_ids: Collection[int] | None = None
    ) -> dict[int, list[RecipeLine]]:
        """Las recetas de esos platos (o de todos). Un plato sin receta no aparece."""
        ...

    async def replace(self, restaurant_id: int, menu_item_id: int, lines: list[RecipeLine]) -> None:
        """Deja exactamente esas líneas; una lista vacía borra la receta."""
        ...
