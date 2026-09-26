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
from resthub.modules.menu.domain.modifiers import (
    MAX_GROUPS,
    MAX_MODIFIER_NAME_LENGTH,
    MAX_OPTIONS,
    ModifierGroup,
    ModifierOption,
)


class ModifierOptionSchema(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_MODIFIER_NAME_LENGTH)
    # Lo que suma al precio del plato; cero si no lo cambia.
    price: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=6, decimal_places=2)


class ModifierGroupSchema(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_MODIFIER_NAME_LENGTH)
    # Mínimo uno: el grupo es obligatorio (por ejemplo, el tamaño).
    min_choices: int = Field(default=0, ge=0, le=MAX_OPTIONS)
    max_choices: int = Field(default=1, ge=1, le=MAX_OPTIONS)
    options: list[ModifierOptionSchema] = Field(min_length=1, max_length=MAX_OPTIONS)

    @classmethod
    def from_entity(cls, group: ModifierGroup) -> ModifierGroupSchema:
        return cls(
            name=group.name,
            min_choices=group.min_choices,
            max_choices=group.max_choices,
            options=[
                ModifierOptionSchema(name=option.name, price=option.price)
                for option in group.options
            ],
        )

    def to_entity(self) -> ModifierGroup:
        return ModifierGroup(
            name=self.name,
            min_choices=self.min_choices,
            max_choices=self.max_choices,
            options=tuple(
                ModifierOption(name=option.name, price=option.price) for option in self.options
            ),
        )


class MenuItemResponse(BaseModel):
    id: int
    category_id: int
    name: str
    description: str
    price: Decimal
    is_available: bool
    is_active: bool
    # Sin insumos para una porción según su receta; no se puede pedir.
    out_of_stock: bool
    position: int
    modifier_groups: list[ModifierGroupSchema]
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
            out_of_stock=item.out_of_stock,
            position=item.position,
            modifier_groups=[ModifierGroupSchema.from_entity(g) for g in item.modifier_groups],
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
    modifier_groups: list[ModifierGroupSchema] = Field(default_factory=list, max_length=MAX_GROUPS)


class UpdateMenuItemRequest(BaseModel):
    category_id: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=MAX_ITEM_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    price: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    is_active: bool | None = None
    is_available: bool | None = None
    # Reemplaza todos los grupos; una lista vacía los quita.
    modifier_groups: list[ModifierGroupSchema] | None = Field(default=None, max_length=MAX_GROUPS)


class SetAvailabilityRequest(BaseModel):
    is_available: bool
