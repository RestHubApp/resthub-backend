"""bloqueo por inactividad con PIN (RFS-29).

`restaurants.idle_lock_minutes`: minutos sin uso tras los que la sesión se
bloquea hasta poner el PIN; 10 por omisión, 0 lo apaga. `users.pin_hash`: el
hash del PIN de 4 a 6 dígitos, nulo mientras la cuenta no elija uno.
Deshacerla borra las dos columnas.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("restaurants") as batch:
        batch.add_column(
            sa.Column("idle_lock_minutes", sa.Integer(), nullable=False, server_default="10")
        )
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("pin_hash", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("pin_hash")
    with op.batch_alter_table("restaurants") as batch:
        batch.drop_column("idle_lock_minutes")
