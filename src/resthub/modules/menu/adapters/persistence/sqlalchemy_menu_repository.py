from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.menu.adapters.persistence.mappers import (
    category_to_entity,
    category_to_row,
    item_to_entity,
    item_to_row,
)
from resthub.modules.menu.adapters.persistence.models import MenuCategoryRow, MenuItemRow
from resthub.modules.menu.domain.entities import MenuCategory, MenuItem
from resthub.modules.menu.domain.exceptions import CategoryNotFound, MenuItemNotFound


class SqlAlchemyMenuRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_category(self, category: MenuCategory) -> MenuCategory:
        row = category_to_row(category)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return category_to_entity(row)

    async def get_category(self, restaurant_id: int, category_id: int) -> MenuCategory | None:
        row = await self._category_row(restaurant_id, category_id)
        return category_to_entity(row) if row else None

    async def find_category_by_name(self, restaurant_id: int, name: str) -> MenuCategory | None:
        result = await self._session.execute(
            select(MenuCategoryRow).where(
                MenuCategoryRow.restaurant_id == restaurant_id,
                func.lower(MenuCategoryRow.name) == name.lower(),
            )
        )
        row = result.scalars().first()
        return category_to_entity(row) if row else None

    async def list_categories(self, restaurant_id: int) -> list[MenuCategory]:
        result = await self._session.execute(
            select(MenuCategoryRow)
            .where(MenuCategoryRow.restaurant_id == restaurant_id)
            .order_by(MenuCategoryRow.position, MenuCategoryRow.id)
        )
        return [category_to_entity(row) for row in result.scalars().all()]

    async def save_category(self, category: MenuCategory) -> MenuCategory:
        row = await self._category_row(category.restaurant_id, category.id or 0)
        if row is None:
            raise CategoryNotFound(category.id or 0)
        row.name = category.name
        row.position = category.position
        row.is_active = category.is_active
        await self._session.flush()
        return category_to_entity(row)

    async def delete_category(self, category: MenuCategory) -> None:
        row = await self._category_row(category.restaurant_id, category.id or 0)
        if row is None:
            raise CategoryNotFound(category.id or 0)
        await self._session.delete(row)
        await self._session.flush()

    async def count_items_in_category(self, restaurant_id: int, category_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(MenuItemRow)
            .where(
                MenuItemRow.restaurant_id == restaurant_id,
                MenuItemRow.category_id == category_id,
            )
        )
        return int(result.scalar_one())

    async def add_item(self, item: MenuItem) -> MenuItem:
        row = item_to_row(item)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return item_to_entity(row)

    async def get_item(self, restaurant_id: int, item_id: int) -> MenuItem | None:
        row = await self._item_row(restaurant_id, item_id)
        return item_to_entity(row) if row else None

    async def find_item_by_name(self, restaurant_id: int, name: str) -> MenuItem | None:
        result = await self._session.execute(
            select(MenuItemRow).where(
                MenuItemRow.restaurant_id == restaurant_id,
                func.lower(MenuItemRow.name) == name.lower(),
            )
        )
        row = result.scalars().first()
        return item_to_entity(row) if row else None

    async def list_items(
        self, restaurant_id: int, category_id: int | None = None
    ) -> list[MenuItem]:
        statement = select(MenuItemRow).where(MenuItemRow.restaurant_id == restaurant_id)
        if category_id is not None:
            statement = statement.where(MenuItemRow.category_id == category_id)
        result = await self._session.execute(
            statement.order_by(MenuItemRow.category_id, MenuItemRow.position, MenuItemRow.id)
        )
        return [item_to_entity(row) for row in result.scalars().all()]

    async def save_item(self, item: MenuItem) -> MenuItem:
        row = await self._item_row(item.restaurant_id, item.id or 0)
        if row is None:
            raise MenuItemNotFound(item.id or 0)
        row.category_id = item.category_id
        row.name = item.name
        row.description = item.description
        row.price = item.price
        row.is_available = item.is_available
        row.is_active = item.is_active
        row.position = item.position
        await self._session.flush()
        return item_to_entity(row)

    async def save_positions(self, elements: list[MenuCategory] | list[MenuItem]) -> None:
        for element in elements:
            if isinstance(element, MenuCategory):
                row: MenuCategoryRow | MenuItemRow | None = await self._category_row(
                    element.restaurant_id, element.id or 0
                )
            else:
                row = await self._item_row(element.restaurant_id, element.id or 0)
            if row is not None:
                row.position = element.position
        await self._session.flush()

    async def _category_row(self, restaurant_id: int, category_id: int) -> MenuCategoryRow | None:
        result = await self._session.execute(
            select(MenuCategoryRow).where(
                MenuCategoryRow.id == category_id, MenuCategoryRow.restaurant_id == restaurant_id
            )
        )
        return result.scalar_one_or_none()

    async def _item_row(self, restaurant_id: int, item_id: int) -> MenuItemRow | None:
        result = await self._session.execute(
            select(MenuItemRow).where(
                MenuItemRow.id == item_id, MenuItemRow.restaurant_id == restaurant_id
            )
        )
        return result.scalar_one_or_none()
