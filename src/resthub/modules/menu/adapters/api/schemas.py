"""Contrato HTTP del menú.

Los precios viajan como `Decimal`, que en JSON se escribe como texto ("12.50"):
un número en coma flotante no representa bien los céntimos.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from resthub.modules.menu.domain.entities import (
    MAX_CATEGORY_NAME_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_ITEM_NAME_LENGTH,
    MenuCategory,
    MenuItem,
    MenuSection,
)


class MenuItemResponse(BaseModel):
    id: int
    category_id: int
    name: str
    description: str
    price: Decimal
    is_available: bool
    is_active: bool
    position: int
    created_at: datetime

    @classmethod
    def from_entity(cls, item: MenuItem) -> MenuItemResponse:
        return cls(
            id=item.id or 0,
            category_id=item.category_id,
            name=item.name,
            description=item.description,
            price=item.price,
            is_available=item.is_available,
            is_active=item.is_active,
            position=item.position,
            created_at=item.created_at,
        )


class MenuCategoryResponse(BaseModel):
    id: int
    name: str
    position: int
    is_active: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, category: MenuCategory) -> MenuCategoryResponse:
        return cls(
            id=category.id or 0,
            name=category.name,
            position=category.position,
            is_active=category.is_active,
            created_at=category.created_at,
        )


class MenuSectionResponse(MenuCategoryResponse):
    items: list[MenuItemResponse]

    @classmethod
    def from_section(cls, section: MenuSection) -> MenuSectionResponse:
        category = section.category
        return cls(
            id=category.id or 0,
            name=category.name,
            position=category.position,
            is_active=category.is_active,
            created_at=category.created_at,
            items=[MenuItemResponse.from_entity(item) for item in section.items],
        )


class MenuResponse(BaseModel):
    categories: list[MenuSectionResponse]


class CreateCategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_CATEGORY_NAME_LENGTH)


class UpdateCategoryRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_CATEGORY_NAME_LENGTH)
    is_active: bool | None = None


class ReorderRequest(BaseModel):
    ids: list[int] = Field(description="Todos los identificadores, en el orden nuevo")


class CreateMenuItemRequest(BaseModel):
    category_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=MAX_ITEM_NAME_LENGTH)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LENGTH)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    is_available: bool = True


class UpdateMenuItemRequest(BaseModel):
    category_id: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=MAX_ITEM_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    price: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    is_active: bool | None = None
    is_available: bool | None = None


class SetAvailabilityRequest(BaseModel):
    is_available: bool
