"""crear restaurantes, usuarios y bitácora.

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "restaurants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=60), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_restaurants_slug", "restaurants", ["slug"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_users_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # El índice único del correo es la única garantía real contra dos cuentas
    # con la misma dirección: la comprobación previa del caso de uso es una
    # cortesía, no una exclusión mutua. Es global y no por restaurante porque
    # el acceso es por correo, sin elegir antes el local.
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_restaurant_id", "users", ["restaurant_id"], unique=False)

    op.create_table(
        "activity_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_activity_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_activity_user", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activity_log_kind", "activity_log", ["kind"], unique=False)
    op.create_index("ix_activity_log_user_id", "activity_log", ["user_id"], unique=False)
    # La pantalla de bitácora siempre pide lo último de un restaurante; este
    # índice también cubre el filtro por restaurante solo, que empieza por él.
    op.create_index(
        "ix_activity_restaurant_moment",
        "activity_log",
        ["restaurant_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_activity_user_moment", "activity_log", ["user_id", "occurred_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_activity_user_moment", table_name="activity_log")
    op.drop_index("ix_activity_restaurant_moment", table_name="activity_log")
    op.drop_index("ix_activity_log_user_id", table_name="activity_log")
    op.drop_index("ix_activity_log_kind", table_name="activity_log")
    op.drop_table("activity_log")
    op.drop_index("ix_users_restaurant_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_restaurants_slug", table_name="restaurants")
    op.drop_table("restaurants")
