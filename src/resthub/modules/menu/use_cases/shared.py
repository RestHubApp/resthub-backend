"""Piezas comunes a los casos de uso del menú."""

from __future__ import annotations

from resthub.core.identity import Role
from resthub.core.realtime import EventPublisher, RealtimeEvent
from resthub.modules.menu.domain.entities import MenuCategory, MenuItem
from resthub.modules.menu.domain.exceptions import CategoryNotFound, MenuItemNotFound
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
            roles=frozenset(Role),
            reference_id=reference_id,
        )
    )


async def find_category(menu: MenuRepository, restaurant_id: int, category_id: int) -> MenuCategory:
    category = await menu.get_category(restaurant_id, category_id)
    if category is None:
        raise CategoryNotFound(category_id)
    return category


async def find_item(menu: MenuRepository, restaurant_id: int, item_id: int) -> MenuItem:
    item = await menu.get_item(restaurant_id, item_id)
    if item is None:
        raise MenuItemNotFound(item_id)
    return item
