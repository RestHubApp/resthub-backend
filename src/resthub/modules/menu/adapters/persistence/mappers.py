"""Traducción entre las filas de las tablas y las entidades de dominio."""

from __future__ import annotations

from resthub.core.timestamps import as_utc
from resthub.modules.menu.adapters.persistence.models import MenuCategoryRow, MenuItemRow
from resthub.modules.menu.domain.entities import MenuCategory, MenuItem


def category_to_entity(row: MenuCategoryRow) -> MenuCategory:
    return MenuCategory(
        id=row.id,
        restaurant_id=row.restaurant_id,
        name=row.name,
        position=row.position,
        is_active=row.is_active,
        created_at=as_utc(row.created_at),
    )


def category_to_row(category: MenuCategory) -> MenuCategoryRow:
    return MenuCategoryRow(
        restaurant_id=category.restaurant_id,
        name=category.name,
        position=category.position,
        is_active=category.is_active,
        created_at=category.created_at,
    )


def item_to_entity(row: MenuItemRow) -> MenuItem:
    return MenuItem(
        id=row.id,
        restaurant_id=row.restaurant_id,
        category_id=row.category_id,
        name=row.name,
        description=row.description,
        price=row.price,
        is_available=row.is_available,
        is_active=row.is_active,
        position=row.position,
        created_at=as_utc(row.created_at),
    )


def item_to_row(item: MenuItem) -> MenuItemRow:
    return MenuItemRow(
        restaurant_id=item.restaurant_id,
        category_id=item.category_id,
        name=item.name,
        description=item.description,
        price=item.price,
        is_available=item.is_available,
        is_active=item.is_active,
        position=item.position,
        created_at=item.created_at,
    )
