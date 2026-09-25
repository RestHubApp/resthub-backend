"""crear el menú: categorías y platos.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "menu_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_menu_categories_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_menu_categories_restaurant_id", "menu_categories", ["restaurant_id"], unique=False
    )

    op.create_table(
        "menu_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_menu_items_restaurant",
            ondelete="RESTRICT",
        ),
        # RESTRICT: una categoría con platos no se borra; el caso de uso lo
        # comprueba antes y la base es la última palabra.
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["menu_categories.id"],
            name="fk_menu_items_category",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_menu_items_restaurant_id", "menu_items", ["restaurant_id"], unique=False)
    op.create_index("ix_menu_items_category_id", "menu_items", ["category_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_menu_items_category_id", table_name="menu_items")
    op.drop_index("ix_menu_items_restaurant_id", table_name="menu_items")
    op.drop_table("menu_items")
    op.drop_index("ix_menu_categories_restaurant_id", table_name="menu_categories")
    op.drop_table("menu_categories")
