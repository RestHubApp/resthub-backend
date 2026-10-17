"""motivo de baja: quién desactivó una cuenta, cuándo y por qué (RFS-18).

`users.deactivated_at`, `deactivated_by` (la cuenta que la desactivó) y
`deactivation_reason`. Reactivar la cuenta los limpia; el historial queda en la
bitácora. Las cuentas desactivadas antes de esta migración quedan sin motivo.
Deshacerla quita las tres columnas.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deactivated_by", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("deactivation_reason", sa.String(200), nullable=False, server_default="")
        )
        batch.create_foreign_key(
            "fk_users_deactivated_by", "users", ["deactivated_by"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_deactivated_by", type_="foreignkey")
        batch.drop_column("deactivation_reason")
        batch.drop_column("deactivated_by")
        batch.drop_column("deactivated_at")
