"""Traducción entre las filas de las tablas y las entidades de dominio."""

from __future__ import annotations

from resthub.core.timestamps import as_utc
from resthub.modules.inventory.adapters.persistence.models import (
    IngredientRow,
    RecipeLineRow,
    StockMovementRow,
)
from resthub.modules.inventory.domain.entities import (
    Ingredient,
    MovementKind,
    RecipeLine,
    StockMovement,
    Unit,
)
from resthub.modules.inventory.domain.packaging import EntryMode, Packaging


def ingredient_to_entity(row: IngredientRow) -> Ingredient:
    return Ingredient(
        id=row.id,
        restaurant_id=row.restaurant_id,
        name=row.name,
        unit=Unit(row.unit),
        min_stock=row.min_stock,
        unit_cost=row.unit_cost,
        is_active=row.is_active,
        packaging=packaging_to_entity(row),
        created_at=as_utc(row.created_at),
    )


def packaging_to_entity(row: IngredientRow) -> Packaging | None:
    if row.package_size is None:
        return None
    return Packaging(
        size=row.package_size,
        label=row.package_label,
        full_weight_g=row.full_weight_g,
        empty_weight_g=row.empty_weight_g,
    )


def copy_packaging(packaging: Packaging | None, row: IngredientRow) -> None:
    row.package_size = packaging.size if packaging else None
    row.package_label = packaging.label if packaging else ""
    row.full_weight_g = packaging.full_weight_g if packaging else None
    row.empty_weight_g = packaging.empty_weight_g if packaging else None


def ingredient_to_row(ingredient: Ingredient) -> IngredientRow:
    row = IngredientRow(
        restaurant_id=ingredient.restaurant_id,
        name=ingredient.name,
        unit=ingredient.unit.value,
        min_stock=ingredient.min_stock,
        unit_cost=ingredient.unit_cost,
        is_active=ingredient.is_active,
        created_at=ingredient.created_at,
    )
    copy_packaging(ingredient.packaging, row)
    return row


def movement_to_entity(row: StockMovementRow) -> StockMovement:
    return StockMovement(
        id=row.id,
        restaurant_id=row.restaurant_id,
        ingredient_id=row.ingredient_id,
        kind=MovementKind(row.kind),
        quantity=row.quantity,
        unit_cost=row.unit_cost,
        reason=row.reason,
        order_id=row.order_id,
        order_item_id=row.order_item_id,
        entry_mode=EntryMode(row.entry_mode) if row.entry_mode else None,
        entry_quantity=row.entry_quantity,
        stock_count_id=row.stock_count_id,
        created_by=row.created_by,
        created_at=as_utc(row.created_at),
    )


def movement_to_row(movement: StockMovement) -> StockMovementRow:
    return StockMovementRow(
        restaurant_id=movement.restaurant_id,
        ingredient_id=movement.ingredient_id,
        kind=movement.kind.value,
        quantity=movement.quantity,
        unit_cost=movement.unit_cost,
        reason=movement.reason,
        order_id=movement.order_id,
        order_item_id=movement.order_item_id,
        entry_mode=movement.entry_mode.value if movement.entry_mode else None,
        entry_quantity=movement.entry_quantity,
        stock_count_id=movement.stock_count_id,
        created_by=movement.created_by,
        created_at=movement.created_at,
    )


def line_to_entity(row: RecipeLineRow) -> RecipeLine:
    return RecipeLine(ingredient_id=row.ingredient_id, quantity=row.quantity)
