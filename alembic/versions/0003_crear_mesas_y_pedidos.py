"""crear mesas y pedidos.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dining_tables",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=30), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_dining_tables_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dining_tables_restaurant_id", "dining_tables", ["restaurant_id"], unique=False
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("table_id", sa.Integer(), nullable=True),
        sa.Column("waiter_id", sa.Integer(), nullable=False),
        sa.Column("customer_name", sa.String(length=80), nullable=False),
        sa.Column("notes", sa.String(length=300), nullable=False),
        sa.Column("total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("cancel_reason", sa.String(length=300), nullable=False),
        sa.Column("payment_method", sa.String(length=20), nullable=True),
        sa.Column("amount_received", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["restaurant_id"], ["restaurants.id"], name="fk_orders_restaurant", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["table_id"], ["dining_tables.id"], name="fk_orders_table", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["waiter_id"], ["users.id"], name="fk_orders_waiter", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        # El correlativo es por restaurante y por día del restaurante. El caso
        # de uso serializa la numeración; este índice es la última palabra.
        sa.UniqueConstraint(
            "restaurant_id", "business_date", "number", name="uq_orders_daily_number"
        ),
    )
    op.create_index(
        "ix_orders_restaurant_day", "orders", ["restaurant_id", "business_date"], unique=False
    )
    op.create_index(
        "ix_orders_restaurant_status", "orders", ["restaurant_id", "status"], unique=False
    )
    op.create_index("ix_orders_table_id", "orders", ["table_id"], unique=False)
    op.create_index("ix_orders_waiter_id", "orders", ["waiter_id"], unique=False)

    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("menu_item_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_order_items_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_order_items_order", ondelete="CASCADE"
        ),
        # RESTRICT: un plato que ya se vendió no se puede borrar del menú, solo
        # desactivar.
        sa.ForeignKeyConstraint(
            ["menu_item_id"],
            ["menu_items.id"],
            name="fk_order_items_menu_item",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_items_restaurant_id", "order_items", ["restaurant_id"], unique=False)
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"], unique=False)
    op.create_index("ix_order_items_menu_item_id", "order_items", ["menu_item_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_order_items_menu_item_id", table_name="order_items")
    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_index("ix_order_items_restaurant_id", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_waiter_id", table_name="orders")
    op.drop_index("ix_orders_table_id", table_name="orders")
    op.drop_index("ix_orders_restaurant_status", table_name="orders")
    op.drop_index("ix_orders_restaurant_day", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_dining_tables_restaurant_id", table_name="dining_tables")
    op.drop_table("dining_tables")
