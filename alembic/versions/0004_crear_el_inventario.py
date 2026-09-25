"""crear el inventario: insumos, libro de movimientos y recetas.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingredients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("unit", sa.String(length=10), nullable=False),
        sa.Column("min_stock", sa.Numeric(precision=12, scale=3), nullable=False),
        # Seis decimales: el costo va por gramo o por mililitro.
        sa.Column("unit_cost", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_ingredients_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingredients_restaurant_id", "ingredients", ["restaurant_id"], unique=False)

    op.create_table(
        "recipe_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("menu_item_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_recipe_lines_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["menu_item_id"],
            ["menu_items.id"],
            name="fk_recipe_lines_menu_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredients.id"],
            name="fk_recipe_lines_ingredient",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("menu_item_id", "ingredient_id", name="uq_recipe_lines_ingredient"),
    )
    op.create_index(
        "ix_recipe_lines_restaurant_id", "recipe_lines", ["restaurant_id"], unique=False
    )
    op.create_index("ix_recipe_lines_menu_item_id", "recipe_lines", ["menu_item_id"], unique=False)
    op.create_index(
        "ix_recipe_lines_ingredient_id", "recipe_lines", ["ingredient_id"], unique=False
    )

    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_stock_movements_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredients.id"],
            name="fk_stock_movements_ingredient",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_stock_movements_order", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"],
            ["order_items.id"],
            name="fk_stock_movements_order_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_stock_movements_user", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Un ítem de pedido descuenta cada insumo una sola vez. NULL no choca
        # con NULL, así que compras, mermas y ajustes no se ven afectados.
        sa.UniqueConstraint(
            "order_item_id", "ingredient_id", name="uq_stock_movements_consumption"
        ),
    )
    op.create_index(
        "ix_stock_movements_ingredient_id", "stock_movements", ["ingredient_id"], unique=False
    )
    op.create_index("ix_stock_movements_order_id", "stock_movements", ["order_id"], unique=False)
    op.create_index(
        "ix_stock_movements_restaurant_moment",
        "stock_movements",
        ["restaurant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_stock_movements_restaurant_moment", table_name="stock_movements")
    op.drop_index("ix_stock_movements_order_id", table_name="stock_movements")
    op.drop_index("ix_stock_movements_ingredient_id", table_name="stock_movements")
    op.drop_table("stock_movements")
    op.drop_index("ix_recipe_lines_ingredient_id", table_name="recipe_lines")
    op.drop_index("ix_recipe_lines_menu_item_id", table_name="recipe_lines")
    op.drop_index("ix_recipe_lines_restaurant_id", table_name="recipe_lines")
    op.drop_table("recipe_lines")
    op.drop_index("ix_ingredients_restaurant_id", table_name="ingredients")
    op.drop_table("ingredients")
