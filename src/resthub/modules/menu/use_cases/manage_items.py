"""Casos de uso de los platos del menú.

Un plato no se borra: se desactiva. Los pedidos ya cobrados y las recetas del
inventario lo siguen nombrando, y borrarlo dejaría esas referencias colgando.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.realtime import EventPublisher
from resthub.modules.menu.domain.entities import MenuItem, reorder, validate_item_name
from resthub.modules.menu.domain.exceptions import MenuItemNameTaken
from resthub.modules.menu.domain.modifiers import ModifierGroup
from resthub.modules.menu.ports.menu_repository import MenuRepository
from resthub.modules.menu.use_cases.shared import announce_menu_change, find_category, find_item


async def _ensure_name_is_free(
    menu: MenuRepository, restaurant_id: int, name: str, own_id: int | None = None
) -> None:
    # El nombre es único en todo el local y no por categoría: es lo que lee el
    # mesero en la comanda y lo que sale en los reportes de platos vendidos.
    existing = await menu.find_item_by_name(restaurant_id, name)
    if existing is not None and existing.id != own_id:
        raise MenuItemNameTaken(name)


@dataclass(frozen=True, slots=True)
class CreateMenuItemCommand:
    restaurant_id: int
    actor_id: int
    category_id: int
    name: str
    price: Decimal
    description: str = ""
    is_available: bool = True
    modifier_groups: tuple[ModifierGroup, ...] = ()


class CreateMenuItem:
    def __init__(
        self, menu: MenuRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._menu = menu
        self._activity = activity
        self._events = events

    async def __call__(self, command: CreateMenuItemCommand) -> MenuItem:
        await find_category(self._menu, command.restaurant_id, command.category_id)
        name = validate_item_name(command.name)
        await _ensure_name_is_free(self._menu, command.restaurant_id, name)
        position = len(await self._menu.list_items(command.restaurant_id, command.category_id))

        created = await self._menu.add_item(
            MenuItem(
                restaurant_id=command.restaurant_id,
                category_id=command.category_id,
                name=name,
                price=command.price,
                description=command.description,
                is_available=command.is_available,
                position=position,
                modifier_groups=command.modifier_groups,
            )
        )
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.MENU_ITEM_CREATED,
            f"{created.name} (S/ {created.price})",
        )
        announce_menu_change(self._events, command.restaurant_id, created.id)
        return created


@dataclass(frozen=True, slots=True)
class UpdateMenuItemCommand:
    restaurant_id: int
    actor_id: int
    item_id: int
    # `None` deja el campo como está.
    category_id: int | None = None
    name: str | None = None
    description: str | None = None
    price: Decimal | None = None
    is_active: bool | None = None
    is_available: bool | None = None
    # Reemplaza todos los grupos; una tupla vacía los quita.
    modifier_groups: tuple[ModifierGroup, ...] | None = None


class UpdateMenuItem:
    def __init__(
        self, menu: MenuRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._menu = menu
        self._activity = activity
        self._events = events

    async def __call__(self, command: UpdateMenuItemCommand) -> MenuItem:
        item = await find_item(self._menu, command.restaurant_id, command.item_id)

        if command.category_id is not None and command.category_id != item.category_id:
            await find_category(self._menu, command.restaurant_id, command.category_id)
            item.category_id = command.category_id
            # Llega al final de su nueva categoría, igual que un plato nuevo.
            item.position = len(
                await self._menu.list_items(command.restaurant_id, command.category_id)
            )
        if command.name is not None:
            name = validate_item_name(command.name)
            await _ensure_name_is_free(self._menu, command.restaurant_id, name, item.id)
            item.rename(name)
        if command.description is not None:
            item.describe(command.description)
        if command.price is not None:
            item.reprice(command.price)
        if command.is_active is not None:
            item.is_active = command.is_active
        if command.is_available is not None:
            item.is_available = command.is_available
        if command.modifier_groups is not None:
            item.set_modifiers(command.modifier_groups)

        saved = await self._menu.save_item(item)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.MENU_ITEM_UPDATED,
            f"{saved.name} (S/ {saved.price})",
        )
        announce_menu_change(self._events, command.restaurant_id, saved.id)
        return saved


@dataclass(frozen=True, slots=True)
class SetAvailabilityCommand:
    restaurant_id: int
    actor_id: int
    item_id: int
    is_available: bool


class SetAvailability:
    """El interruptor rápido de "hay / no hay hoy".

    Va aparte de la edición para que la bitácora distinga "se acabó el
    ceviche" de "le cambiaron el precio al ceviche".
    """

    def __init__(
        self, menu: MenuRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._menu = menu
        self._activity = activity
        self._events = events

    async def __call__(self, command: SetAvailabilityCommand) -> MenuItem:
        item = await find_item(self._menu, command.restaurant_id, command.item_id)
        if item.is_available == command.is_available:
            return item
        item.is_available = command.is_available
        saved = await self._menu.save_item(item)
        state = "disponible" if saved.is_available else "agotado"
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.MENU_ITEM_AVAILABILITY_CHANGED,
            f"{saved.name}: {state}",
        )
        announce_menu_change(self._events, command.restaurant_id, saved.id)
        return saved


@dataclass(frozen=True, slots=True)
class ReorderItemsCommand:
    restaurant_id: int
    category_id: int
    item_ids: list[int]


class ReorderItems:
    """Ordena los platos de una categoría."""

    def __init__(self, menu: MenuRepository, events: EventPublisher) -> None:
        self._menu = menu
        self._events = events

    async def __call__(self, command: ReorderItemsCommand) -> list[MenuItem]:
        await find_category(self._menu, command.restaurant_id, command.category_id)
        items = await self._menu.list_items(command.restaurant_id, command.category_id)
        ordered = reorder(items, command.item_ids)
        await self._menu.save_positions(ordered)
        announce_menu_change(self._events, command.restaurant_id, command.category_id)
        return ordered
