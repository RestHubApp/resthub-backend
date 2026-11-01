"""Piezas comunes a los casos de uso del menú."""

from __future__ import annotations

from resthub.core.realtime import EventPublisher, RealtimeEvent
from resthub.modules.menu.domain.entities import MenuCategory, MenuItem
from resthub.modules.menu.domain.exceptions import CategoryNotFoundError, MenuItemNotFoundError
from resthub.modules.menu.ports.menu_repository import MenuRepository

# El celular del mesero escucha este tema para volver a pedir el menú: si el
# encargado marca "se acabó el ceviche", nadie más lo ofrece en la mesa.
MENU_TOPIC = "menu"


def announce_menu_change(
    events: EventPublisher, restaurant_id: int, reference_id: int | None
) -> None:
    events.publish(
        RealtimeEvent(
            restaurant_id=restaurant_id,
            topic=MENU_TOPIC,
            everyone=True,
            reference_id=reference_id,
        )
    )


async def find_category(menu: MenuRepository, restaurant_id: int, category_id: int) -> MenuCategory:
    category = await menu.get_category(restaurant_id, category_id)
    if category is None:
        raise CategoryNotFoundError(category_id)
    return category


async def find_item(menu: MenuRepository, restaurant_id: int, item_id: int) -> MenuItem:
    item = await menu.get_item(restaurant_id, item_id)
    if item is None:
        raise MenuItemNotFoundError(item_id)
    return item


async def find_listed_item(
    menu: MenuRepository, restaurant_id: int, item_id: int, *, include_inactive: bool = False
) -> MenuItem:
    """El plato tal como lo ve la carta.

    Sin `include_inactive`, uno desactivado o de una categoría desactivada no
    existe, igual que en `ReadMenu`.
    """
    item = await find_item(menu, restaurant_id, item_id)
    if include_inactive:
        return item
    category = await menu.get_category(restaurant_id, item.category_id)
    if not item.is_active or category is None or not category.is_active:
        raise MenuItemNotFoundError(item_id)
    return item
