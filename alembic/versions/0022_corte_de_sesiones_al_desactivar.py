"""cortar las sesiones también al desactivar una cuenta.

`users.password_changed_at` pasa a llamarse `sessions_valid_since`: ya no sella
solo el cambio de contraseña, también la desactivación, para que reactivar la
cuenta no devuelva la vida a los tokens que tenía abiertos. La misma columna
se agrega a `platform_admins`, cuyos tokens se cortan igual. Deshacerla
devuelve el nombre anterior y quita la columna nueva.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("password_changed_at", new_column_name="sessions_valid_since")
    with op.batch_alter_table("platform_admins") as batch:
        batch.add_column(
            sa.Column("sessions_valid_since", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("platform_admins") as batch:
        batch.drop_column("sessions_valid_since")
    with op.batch_alter_table("users") as batch:
        batch.alter_column("sessions_valid_since", new_column_name="password_changed_at")
