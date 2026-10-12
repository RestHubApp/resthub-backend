"""Casos de uso del menú con un repositorio en memoria: qué cambia, qué se anota y qué se avisa."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.modules.menu.domain.entities import MenuCategory, MenuItem
from resthub.modules.menu.domain.exceptions import (
    CategoryNameTakenError,
    CategoryNotEmptyError,
    CategoryNotFoundError,
    InvalidMenuItemError,
    MenuItemNameTakenError,
    MenuItemNotFoundError,
)
from resthub.modules.menu.domain.modifiers import ModifierGroup, ModifierOption
from resthub.modules.menu.use_cases.manage_categories import (
    CreateCategory,
    CreateCategoryCommand,
    DeleteCategory,
    DeleteCategoryCommand,
    UpdateCategory,
    UpdateCategoryCommand,
)
from resthub.modules.menu.use_cases.manage_items import (
    CreateMenuItem,
    CreateMenuItemCommand,
    SetAvailability,
    SetAvailabilityCommand,
    UpdateMenuItem,
    UpdateMenuItemCommand,
)
from resthub.modules.menu.use_cases.read_menu import ReadMenu, ReadMenuItem, ReadMenuQuery
from resthub.modules.menu.use_cases.shared import MENU_TOPIC
from tests.fakes import RecordingEvents

RESTAURANT = 1
OTHER_RESTAURANT = 2
OWNER = 10


class MemoryMenu:
    """Solo lo que usan estos casos de uso; los nombres se comparan sin mayúsculas."""

    def __init__(self) -> None:
        self.categories: dict[int, MenuCategory] = {}
        self.items: dict[int, MenuItem] = {}

    async def add_category(self, category: MenuCategory) -> MenuCategory:
        stored = replace(category, id=len(self.categories) + 1)
        self.categories[stored.id or 0] = stored
        return replace(stored)

    async def get_category(self, restaurant_id: int, category_id: int) -> MenuCategory | None:
        found = self.categories.get(category_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def find_category_by_name(self, restaurant_id: int, name: str) -> MenuCategory | None:
        return next(
            (
                replace(c)
                for c in self.categories.values()
                if c.restaurant_id == restaurant_id and c.name.casefold() == name.casefold()
            ),
            None,
        )

    async def list_categories(self, restaurant_id: int) -> list[MenuCategory]:
        return sorted(
            (replace(c) for c in self.categories.values() if c.restaurant_id == restaurant_id),
            key=lambda c: c.position,
        )

    async def save_category(self, category: MenuCategory) -> MenuCategory:
        self.categories[category.id or 0] = replace(category)
        return replace(category)

    async def delete_category(self, category: MenuCategory) -> None:
        self.categories.pop(category.id or 0)

    async def count_items_in_category(self, restaurant_id: int, category_id: int) -> int:
        return len(await self.list_items(restaurant_id, category_id))

    async def add_item(self, item: MenuItem) -> MenuItem:
        stored = replace(item, id=100 + len(self.items))
        self.items[stored.id or 0] = stored
        return replace(stored)

    async def get_item(self, restaurant_id: int, item_id: int) -> MenuItem | None:
        found = self.items.get(item_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def find_item_by_name(self, restaurant_id: int, name: str) -> MenuItem | None:
        return next(
            (
                replace(i)
                for i in self.items.values()
                if i.restaurant_id == restaurant_id and i.name.casefold() == name.casefold()
            ),
            None,
        )

    async def list_items(
        self, restaurant_id: int, category_id: int | None = None
    ) -> list[MenuItem]:
        return sorted(
            (
                replace(i)
                for i in self.items.values()
                if i.restaurant_id == restaurant_id
                and (category_id is None or i.category_id == category_id)
            ),
            key=lambda i: (i.category_id, i.position),
        )

    async def save_item(self, item: MenuItem) -> MenuItem:
        self.items[item.id or 0] = replace(item)
        return replace(item)


class ActivityLog:
    def __init__(self) -> None:
        self.entries: list[tuple[int, int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((restaurant_id, user_id, kind, detail))


class OutOfStock:
    def __init__(self, *ids: int) -> None:
        self.ids = frozenset(ids)

    async def out_of_stock(self, restaurant_id: int) -> frozenset[int]:
        return self.ids


@pytest.fixture
def menu() -> MemoryMenu:
    return MemoryMenu()


@pytest.fixture
def activity_log() -> ActivityLog:
    return ActivityLog()


@pytest.fixture
def events() -> RecordingEvents:
    return RecordingEvents()


async def _category(menu: MemoryMenu, name: str, **extra: object) -> MenuCategory:
    return await menu.add_category(MenuCategory(restaurant_id=RESTAURANT, name=name, **extra))  # type: ignore[arg-type]


async def _dish(menu: MemoryMenu, category_id: int, name: str, **extra: object) -> MenuItem:
    return await menu.add_item(
        MenuItem(
            restaurant_id=RESTAURANT,
            category_id=category_id,
            name=name,
            price=Decimal("20.00"),
            **extra,  # type: ignore[arg-type]
        )
    )


# -- Categorías ---------------------------------------------------------------------


async def test_new_category_goes_last_and_is_logged(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    await _category(menu, "Entradas")
    await _category(menu, "Fondos", position=1)

    created = await CreateCategory(menu, activity_log, events)(
        CreateCategoryCommand(RESTAURANT, OWNER, "  Bebidas  ")
    )

    assert (created.name, created.position) == ("Bebidas", 2)
    assert activity_log.entries == [
        (RESTAURANT, OWNER, ActivityKind.MENU_CATEGORY_CREATED, "Bebidas")
    ]
    (event,) = events.published
    assert (event.restaurant_id, event.topic, event.everyone, event.reference_id) == (
        RESTAURANT,
        MENU_TOPIC,
        True,
        created.id,
    )


async def test_category_does_not_repeat_another_ones_name(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    await _category(menu, "Fondos")

    with pytest.raises(CategoryNameTakenError):
        await CreateCategory(menu, activity_log, events)(
            CreateCategoryCommand(RESTAURANT, OWNER, "fondos")
        )
    assert activity_log.entries == []
    assert events.published == []


async def test_renaming_a_category_leaves_its_active_flag_alone(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos", is_active=False)

    saved = await UpdateCategory(menu, activity_log, events)(
        UpdateCategoryCommand(RESTAURANT, OWNER, mains.id or 0, name=" Segundos ")
    )

    assert saved.name == "Segundos"
    assert saved.is_active is False
    assert menu.categories[mains.id or 0].name == "Segundos"
    assert activity_log.entries[-1][2:] == (
        ActivityKind.MENU_CATEGORY_UPDATED,
        "Segundos (inactiva)",
    )
    assert events.published[-1].reference_id == mains.id


async def test_category_can_keep_its_own_name(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos")

    saved = await UpdateCategory(menu, activity_log, events)(
        UpdateCategoryCommand(RESTAURANT, OWNER, mains.id or 0, name="FONDOS", is_active=True)
    )

    assert saved.name == "FONDOS"
    assert activity_log.entries[-1][3] == "FONDOS (activa)"


async def test_renaming_a_category_to_another_ones_name_conflicts(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    await _category(menu, "Fondos")
    drinks = await _category(menu, "Bebidas")

    with pytest.raises(CategoryNameTakenError):
        await UpdateCategory(menu, activity_log, events)(
            UpdateCategoryCommand(RESTAURANT, OWNER, drinks.id or 0, name="Fondos")
        )
    assert menu.categories[drinks.id or 0].name == "Bebidas"
    assert activity_log.entries == []


async def test_category_of_another_restaurant_does_not_exist(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    foreign_category = await menu.add_category(
        MenuCategory(restaurant_id=OTHER_RESTAURANT, name="Postres")
    )

    with pytest.raises(CategoryNotFoundError):
        await UpdateCategory(menu, activity_log, events)(
            UpdateCategoryCommand(RESTAURANT, OWNER, foreign_category.id or 0, is_active=False)
        )
    with pytest.raises(CategoryNotFoundError):
        await DeleteCategory(menu, activity_log, events)(
            DeleteCategoryCommand(RESTAURANT, OWNER, foreign_category.id or 0)
        )
    assert menu.categories[foreign_category.id or 0].is_active is True


async def test_only_a_category_without_dishes_is_deleted(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos")
    empty = await _category(menu, "Temporada")
    await _dish(menu, mains.id or 0, "Lomo saltado")
    delete = DeleteCategory(menu, activity_log, events)

    with pytest.raises(CategoryNotEmptyError):
        await delete(DeleteCategoryCommand(RESTAURANT, OWNER, mains.id or 0))
    await delete(DeleteCategoryCommand(RESTAURANT, OWNER, empty.id or 0))

    assert set(menu.categories) == {mains.id}
    assert activity_log.entries == [
        (RESTAURANT, OWNER, ActivityKind.MENU_CATEGORY_DELETED, "Temporada")
    ]


# -- Platos ---------------------------------------------------------------------


async def test_new_dish_goes_last_in_its_category(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos")
    await _dish(menu, mains.id or 0, "Lomo saltado")

    created = await CreateMenuItem(menu, activity_log, events)(
        CreateMenuItemCommand(
            RESTAURANT, OWNER, mains.id or 0, " Ají  de gallina ", Decimal(22), is_available=False
        )
    )

    assert (created.name, created.position, created.is_available) == ("Ají de gallina", 1, False)
    assert activity_log.entries[-1][2:] == (
        ActivityKind.MENU_ITEM_CREATED,
        "Ají de gallina (S/ 22.00)",
    )


async def test_dish_is_not_created_in_a_foreign_category_or_with_a_repeated_name(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos")
    foreign_category = await menu.add_category(
        MenuCategory(restaurant_id=OTHER_RESTAURANT, name="Fondos")
    )
    await _dish(menu, mains.id or 0, "Lomo saltado")
    create = CreateMenuItem(menu, activity_log, events)

    with pytest.raises(CategoryNotFoundError):
        await create(
            CreateMenuItemCommand(RESTAURANT, OWNER, foreign_category.id or 0, "Seco", Decimal(20))
        )
    with pytest.raises(MenuItemNameTakenError):
        await create(
            CreateMenuItemCommand(RESTAURANT, OWNER, mains.id or 0, "LOMO SALTADO", Decimal(20))
        )
    assert len(menu.items) == 1


async def test_editing_a_dish_changes_only_the_sent_fields(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos")
    lomo = await _dish(menu, mains.id or 0, "Lomo saltado", description="Con papas")
    size = ModifierGroup("Tamaño", (ModifierOption("Personal"), ModifierOption("Familiar")))

    saved = await UpdateMenuItem(menu, activity_log, events)(
        UpdateMenuItemCommand(
            RESTAURANT,
            OWNER,
            lomo.id or 0,
            category_id=mains.id,
            name="Lomo fino",
            description="  Con arroz ",
            is_available=False,
            modifier_groups=(size,),
        )
    )

    assert saved.name == "Lomo fino"
    assert saved.description == "Con arroz"
    assert saved.price == Decimal("20.00")
    assert saved.is_active is True
    assert saved.is_available is False
    assert [g.name for g in saved.modifier_groups] == ["Tamaño"]
    assert saved.position == 0
    assert activity_log.entries[-1][2:] == (ActivityKind.MENU_ITEM_UPDATED, "Lomo fino (S/ 20.00)")


async def test_moving_a_dish_puts_it_last_in_the_other_category(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos")
    drinks = await _category(menu, "Bebidas")
    lomo = await _dish(menu, mains.id or 0, "Lomo saltado")
    await _dish(menu, drinks.id or 0, "Chicha morada")
    await _dish(menu, drinks.id or 0, "Inca Kola", position=1)

    moved = await UpdateMenuItem(menu, activity_log, events)(
        UpdateMenuItemCommand(RESTAURANT, OWNER, lomo.id or 0, category_id=drinks.id)
    )

    assert (moved.category_id, moved.position) == (drinks.id, 2)


async def test_editing_a_dish_with_a_taken_or_invalid_name_does_not_save(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos")
    lomo = await _dish(menu, mains.id or 0, "Lomo saltado")
    await _dish(menu, mains.id or 0, "Seco de res")
    edit = UpdateMenuItem(menu, activity_log, events)

    with pytest.raises(MenuItemNameTakenError):
        await edit(UpdateMenuItemCommand(RESTAURANT, OWNER, lomo.id or 0, name="seco de res"))
    with pytest.raises(InvalidMenuItemError):
        await edit(UpdateMenuItemCommand(RESTAURANT, OWNER, lomo.id or 0, name="  "))
    with pytest.raises(CategoryNotFoundError):
        await edit(UpdateMenuItemCommand(RESTAURANT, OWNER, lomo.id or 0, category_id=999))
    with pytest.raises(MenuItemNotFoundError):
        await edit(UpdateMenuItemCommand(OTHER_RESTAURANT, OWNER, lomo.id or 0, name="Otro"))
    assert menu.items[lomo.id or 0].name == "Lomo saltado"
    assert activity_log.entries == []


async def test_marking_sold_out_twice_does_not_duplicate_the_log(
    menu: MemoryMenu, activity_log: ActivityLog, events: RecordingEvents
) -> None:
    mains = await _category(menu, "Fondos")
    lomo = await _dish(menu, mains.id or 0, "Lomo saltado")
    change = SetAvailability(menu, activity_log, events)

    await change(SetAvailabilityCommand(RESTAURANT, OWNER, lomo.id or 0, is_available=False))
    repeated = await change(
        SetAvailabilityCommand(RESTAURANT, OWNER, lomo.id or 0, is_available=False)
    )
    await change(SetAvailabilityCommand(RESTAURANT, OWNER, lomo.id or 0, is_available=True))

    assert repeated.is_available is False
    assert [detail for *_, detail in activity_log.entries] == [
        "Lomo saltado: agotado",
        "Lomo saltado: disponible",
    ]
    assert len(events.published) == 2


# -- Lectura --------------------------------------------------------------------------


async def test_menu_marks_sold_out_what_stock_cannot_cover(menu: MemoryMenu) -> None:
    mains = await _category(menu, "Fondos")
    inactive_category = await _category(menu, "Temporada", is_active=False)
    lomo = await _dish(menu, mains.id or 0, "Lomo saltado")
    stew = await _dish(menu, mains.id or 0, "Seco de res", position=1)
    await _dish(menu, mains.id or 0, "Ceviche", position=2, is_active=False)
    await _dish(menu, inactive_category.id or 0, "Causa")

    waiter_menu = await ReadMenu(menu, OutOfStock(stew.id or 0))(ReadMenuQuery(RESTAURANT))
    owner_menu = await ReadMenu(menu)(ReadMenuQuery(RESTAURANT, include_inactive=True))

    (section,) = waiter_menu
    assert section.category.id == mains.id
    assert [(p.id, p.out_of_stock, p.can_be_ordered) for p in section.items] == [
        (lomo.id, False, True),
        (stew.id, True, False),
    ]
    assert [len(s.items) for s in owner_menu] == [3, 1]
    assert not any(p.out_of_stock for s in owner_menu for p in s.items)


async def test_category_without_dishes_appears_empty(menu: MemoryMenu) -> None:
    await _category(menu, "Postres")

    (section,) = await ReadMenu(menu)(ReadMenuQuery(RESTAURANT))

    assert section.items == []


async def test_inactive_dish_is_only_seen_by_whoever_manages(menu: MemoryMenu) -> None:
    mains = await _category(menu, "Fondos")
    lomo = await _dish(menu, mains.id or 0, "Lomo saltado")
    ceviche = await _dish(menu, mains.id or 0, "Ceviche", is_active=False)
    read = ReadMenuItem(menu)

    assert (await read(RESTAURANT, lomo.id or 0)).name == "Lomo saltado"
    assert (await read(RESTAURANT, ceviche.id or 0, include_inactive=True)).name == "Ceviche"
    with pytest.raises(MenuItemNotFoundError):
        await read(RESTAURANT, ceviche.id or 0)
    with pytest.raises(MenuItemNotFoundError):
        await read(OTHER_RESTAURANT, lomo.id or 0, include_inactive=True)
