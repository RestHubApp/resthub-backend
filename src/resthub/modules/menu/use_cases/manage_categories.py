"""Casos de uso de las categorías del menú (entradas, fondos, bebidas...)."""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.realtime import EventPublisher
from resthub.modules.menu.domain.entities import MenuCategory, reorder, validate_category_name
from resthub.modules.menu.domain.exceptions import CategoryNameTaken, CategoryNotEmpty
from resthub.modules.menu.ports.menu_repository import MenuRepository
from resthub.modules.menu.use_cases.shared import announce_menu_change, find_category


async def _ensure_name_is_free(
    menu: MenuRepository, restaurant_id: int, name: str, own_id: int | None = None
) -> None:
    existing = await menu.find_category_by_name(restaurant_id, name)
    if existing is not None and existing.id != own_id:
        raise CategoryNameTaken(name)


@dataclass(frozen=True, slots=True)
class CreateCategoryCommand:
    restaurant_id: int
    actor_id: int
    name: str


class CreateCategory:
    def __init__(
        self, menu: MenuRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._menu = menu
        self._activity = activity
        self._events = events

    async def __call__(self, command: CreateCategoryCommand) -> MenuCategory:
        name = validate_category_name(command.name)
        await _ensure_name_is_free(self._menu, command.restaurant_id, name)
        # Una categoría nueva va al final: es lo que espera quien la acaba de
        # crear, y no desordena la carta que ya está armada.
        position = len(await self._menu.list_categories(command.restaurant_id))
        created = await self._menu.add_category(
            MenuCategory(restaurant_id=command.restaurant_id, name=name, position=position)
        )
        await self._activity.record(
            command.restaurant_id, command.actor_id, ActivityKind.MENU_CATEGORY_CREATED, name
        )
        announce_menu_change(self._events, command.restaurant_id, created.id)
        return created


@dataclass(frozen=True, slots=True)
class UpdateCategoryCommand:
    restaurant_id: int
    actor_id: int
    category_id: int
    # `None` deja el campo como está.
    name: str | None = None
    is_active: bool | None = None


class UpdateCategory:
    def __init__(
        self, menu: MenuRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._menu = menu
        self._activity = activity
        self._events = events

    async def __call__(self, command: UpdateCategoryCommand) -> MenuCategory:
        category = await find_category(self._menu, command.restaurant_id, command.category_id)
        if command.name is not None:
            name = validate_category_name(command.name)
            await _ensure_name_is_free(self._menu, command.restaurant_id, name, category.id)
            category.rename(name)
        if command.is_active is not None:
            category.is_active = command.is_active

        saved = await self._menu.save_category(category)
        state = "activa" if saved.is_active else "inactiva"
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.MENU_CATEGORY_UPDATED,
            f"{saved.name} ({state})",
        )
        announce_menu_change(self._events, command.restaurant_id, saved.id)
        return saved


@dataclass(frozen=True, slots=True)
class DeleteCategoryCommand:
    restaurant_id: int
    actor_id: int
    category_id: int


class DeleteCategory:
    """Solo se borra una categoría vacía.

    Con platos adentro habría que decidir qué pasa con ellos, y esa decisión es
    del encargado: moverlos a otra categoría o desactivar esta.
    """

    def __init__(
        self, menu: MenuRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._menu = menu
        self._activity = activity
        self._events = events

    async def __call__(self, command: DeleteCategoryCommand) -> None:
        category = await find_category(self._menu, command.restaurant_id, command.category_id)
        if await self._menu.count_items_in_category(command.restaurant_id, command.category_id):
            raise CategoryNotEmpty(command.category_id)
        await self._menu.delete_category(category)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.MENU_CATEGORY_DELETED,
            category.name,
        )
        announce_menu_change(self._events, command.restaurant_id, category.id)


@dataclass(frozen=True, slots=True)
class ReorderCategoriesCommand:
    restaurant_id: int
    category_ids: list[int]


class ReorderCategories:
    def __init__(self, menu: MenuRepository, events: EventPublisher) -> None:
        self._menu = menu
        self._events = events

    async def __call__(self, command: ReorderCategoriesCommand) -> list[MenuCategory]:
        categories = await self._menu.list_categories(command.restaurant_id)
        ordered = reorder(categories, command.category_ids)
        await self._menu.save_positions(ordered)
        announce_menu_change(self._events, command.restaurant_id, None)
        return ordered
