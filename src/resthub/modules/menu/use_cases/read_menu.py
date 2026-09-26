from __future__ import annotations

from dataclasses import dataclass

from resthub.modules.menu.domain.entities import MenuItem, MenuSection
from resthub.modules.menu.domain.exceptions import MenuItemNotFound
from resthub.modules.menu.ports.menu_repository import MenuRepository
from resthub.modules.menu.ports.stock_availability import StockAvailability
from resthub.modules.menu.use_cases.shared import find_item


@dataclass(frozen=True, slots=True)
class ReadMenuQuery:
    restaurant_id: int
    # Solo quien administra el menú ve lo desactivado. El mesero ve la carta
    # vigente, y dentro de ella qué platos no hay hoy.
    include_inactive: bool = False


class ReadMenu:
    """El menú completo agrupado por categoría, en el orden de la carta."""

    def __init__(self, menu: MenuRepository, stock: StockAvailability | None = None) -> None:
        self._menu = menu
        self._stock = stock

    async def __call__(self, query: ReadMenuQuery) -> list[MenuSection]:
        categories = await self._menu.list_categories(query.restaurant_id)
        items = await self._menu.list_items(query.restaurant_id)
        # Un plato cuya receta pide más insumo del que hay se muestra agotado,
        # sin que nadie tenga que marcarlo: el stock ya lo dice.
        agotados = (
            await self._stock.out_of_stock(query.restaurant_id) if self._stock else frozenset()
        )
        for item in items:
            item.out_of_stock = item.id in agotados

        by_category: dict[int, list[MenuItem]] = {}
        for item in items:
            if query.include_inactive or item.is_active:
                by_category.setdefault(item.category_id, []).append(item)

        return [
            MenuSection(category=category, items=by_category.get(category.id or 0, []))
            for category in categories
            if query.include_inactive or category.is_active
        ]


class ReadMenuItem:
    def __init__(self, menu: MenuRepository) -> None:
        self._menu = menu

    async def __call__(
        self, restaurant_id: int, item_id: int, include_inactive: bool = False
    ) -> MenuItem:
        item = await find_item(self._menu, restaurant_id, item_id)
        if not include_inactive and not item.is_active:
            raise MenuItemNotFound(item_id)
        return item
