"""administración del sistema.

- `platform_admins`: las cuentas del equipo de RestHub. Tabla propia y no filas
  de `users`: no tienen restaurante ni rol, así que ninguna consulta del
  personal ni ningún permiso de un local las alcanza.
- `platform_activity`: la bitácora de lo que hacen, aparte de la de cada local.

Deshacerla borra las dos tablas; los restaurantes y cuentas que se dieron de
alta desde la plataforma quedan como si se hubieran creado con el script.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_admins",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_platform_admins_email", "platform_admins", ["email"], unique=True)

    op.create_table(
        "platform_activity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["platform_admins.id"],
            name="fk_platform_activity_admin",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_activity_admin_id", "platform_activity", ["admin_id"], unique=False
    )
    op.create_index(
        "ix_platform_activity_created_at", "platform_activity", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_platform_activity_created_at", table_name="platform_activity")
    op.drop_index("ix_platform_activity_admin_id", table_name="platform_activity")
    op.drop_table("platform_activity")
    op.drop_index("ix_platform_admins_email", table_name="platform_admins")
    op.drop_table("platform_admins")
