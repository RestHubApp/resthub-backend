from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base


class IngredientRow(Base):
    __tablename__ = "ingredients"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Claves foráneas por nombre de tabla y no por modelo: importar los modelos
    # de otros módulos rompería la independencia.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_ingredients_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(10))
    min_stock: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(precision=14, scale=6))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class StockMovementRow(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Sin índice propio: lo cubre el compuesto de abajo, que empieza por él.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_stock_movements_restaurant", ondelete="RESTRICT")
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", name="fk_stock_movements_ingredient", ondelete="RESTRICT"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(precision=14, scale=6), nullable=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", name="fk_stock_movements_order", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_items.id", name="fk_stock_movements_order_item", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_stock_movements_user", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        # Un ítem de pedido descuenta cada insumo una sola vez. El caso de uso
        # ya lo evita; la base es la última palabra si dos avisos se cruzan.
        # Los movimientos que no son consumos llevan el ítem en NULL, y NULL
        # no choca con NULL en un índice único.
        UniqueConstraint("order_item_id", "ingredient_id", name="uq_stock_movements_consumption"),
        Index("ix_stock_movements_restaurant_moment", "restaurant_id", "created_at"),
    )


class RecipeLineRow(Base):
    __tablename__ = "recipe_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_recipe_lines_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    menu_item_id: Mapped[int] = mapped_column(
        ForeignKey("menu_items.id", name="fk_recipe_lines_menu_item", ondelete="RESTRICT"),
        index=True,
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", name="fk_recipe_lines_ingredient", ondelete="RESTRICT"),
        index=True,
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))

    __table_args__ = (
        UniqueConstraint("menu_item_id", "ingredient_id", name="uq_recipe_lines_ingredient"),
    )
