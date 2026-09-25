"""Puerto de persistencia del menú.

Toda operación recibe el restaurante: una categoría o un plato de otro local
responde como inexistente, sin que el caso de uso tenga que acordarse de
comparar identificadores.
"""

from __future__ import annotations

from typing import Protocol

from resthub.modules.menu.domain.entities import MenuCategory, MenuItem


class MenuRepository(Protocol):
    async def add_category(self, category: MenuCategory) -> MenuCategory: ...

    async def get_category(self, restaurant_id: int, category_id: int) -> MenuCategory | None: ...

    async def find_category_by_name(self, restaurant_id: int, name: str) -> MenuCategory | None:
        """Sin distinguir mayúsculas: "Bebidas" y "bebidas" son la misma categoría."""
        ...

    async def list_categories(self, restaurant_id: int) -> list[MenuCategory]:
        """Todas, activas o no, ordenadas por posición."""
        ...

    async def save_category(self, category: MenuCategory) -> MenuCategory: ...

    async def delete_category(self, category: MenuCategory) -> None: ...

    async def count_items_in_category(self, restaurant_id: int, category_id: int) -> int: ...

    async def add_item(self, item: MenuItem) -> MenuItem: ...

    async def get_item(self, restaurant_id: int, item_id: int) -> MenuItem | None: ...

    async def find_item_by_name(self, restaurant_id: int, name: str) -> MenuItem | None: ...

    async def list_items(
        self, restaurant_id: int, category_id: int | None = None
    ) -> list[MenuItem]:
        """Todos, activos o no, ordenados por categoría y posición."""
        ...

    async def save_item(self, item: MenuItem) -> MenuItem: ...

    async def save_positions(self, elements: list[MenuCategory] | list[MenuItem]) -> None: ...
