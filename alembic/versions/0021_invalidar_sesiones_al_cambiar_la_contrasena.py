"""invalidar las sesiones abiertas al cambiar la contraseña.

`users.password_changed_at` dice desde cuándo vale la contraseña actual: un
token emitido antes de esa fecha deja de servir. Nula en las cuentas que nunca
la cambiaron. Deshacerla quita la columna.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("password_changed_at")
