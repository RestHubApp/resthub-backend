"""horario de atención y accesos fuera de él (RFS-32).

`restaurants.opening_hours`: los tramos del horario, `[{day, opens, closes}]`
en la hora del local; NULL, sin horario. `access_history.outside_hours`: el
intento cayó fuera del horario del local de la cuenta. Los accesos anteriores
quedan sin marcar. Deshacerla quita las dos columnas.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("restaurants") as batch:
        batch.add_column(sa.Column("opening_hours", sa.JSON(), nullable=True))
    with op.batch_alter_table("access_history") as batch:
        batch.add_column(
            sa.Column("outside_hours", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("access_history") as batch:
        batch.drop_column("outside_hours")
    with op.batch_alter_table("restaurants") as batch:
        batch.drop_column("opening_hours")
