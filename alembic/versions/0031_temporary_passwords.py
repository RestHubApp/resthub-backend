"""contraseña temporal: la cuenta la cambia antes de hacer cualquier otra cosa (RFS-20).

`users.must_change_password` se pone en verdadero cuando otra persona fija la
contraseña (alta del personal, alta de un encargado desde la plataforma,
restablecimiento) y vuelve a falso cuando la cuenta elige la suya. Las cuentas
que ya existen quedan en falso: su contraseña no cambia. Deshacerla quita la
columna.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("must_change_password")
