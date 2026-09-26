"""local de muestra y vista previa.

- `restaurants.is_sandbox`: marca el local de muestra con el que la
  administración del sistema abre la aplicación como un encargado o un mesero.
  Los que ya existían son restaurantes reales (`false`). Un índice único
  parcial deja a lo sumo uno vigente (`WHERE is_sandbox AND is_active`).
- `preview_codes`: los códigos de un solo uso que canjea `POST /auth/preview`.
  Se guarda el SHA-256 del código, nunca el código.

Deshacerla borra la tabla de códigos, el índice y la columna. Antes desactiva
los locales de muestra: sin la marca serían restaurantes comunes y activos, con
cuentas sin contraseña utilizable y un acceso con contraseña que ya no los
rechaza.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("restaurants") as batch:
        batch.add_column(
            sa.Column("is_sandbox", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    # Índice único parcial, que SQLite y PostgreSQL entienden igual.
    op.create_index(
        "uq_restaurants_one_active_sandbox",
        "restaurants",
        ["is_sandbox"],
        unique=True,
        sqlite_where=sa.text("is_sandbox AND is_active"),
        postgresql_where=sa.text("is_sandbox AND is_active"),
    )

    op.create_table(
        "preview_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("platform_admin_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_preview_codes_user", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["platform_admin_id"],
            ["platform_admins.id"],
            name="fk_preview_codes_admin",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_preview_codes_code_hash", "preview_codes", ["code_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_preview_codes_code_hash", table_name="preview_codes")
    op.drop_table("preview_codes")
    # Sin la marca, un local de muestra quedaría como un restaurante real y
    # activo. Desactivado, nadie entra y la plataforma lo ve como archivado.
    op.execute(
        sa.text("UPDATE restaurants SET is_active = :off WHERE is_sandbox = :on").bindparams(
            off=False, on=True
        )
    )
    op.drop_index("uq_restaurants_one_active_sandbox", table_name="restaurants")
    with op.batch_alter_table("restaurants") as batch:
        batch.drop_column("is_sandbox")
