"""borrar los datos de un cliente (derecho de cancelación).

Ley N.º 29733: `customers.anonymized_at` marca a quien pidió borrar sus datos.
Su ficha queda sin nada que lo identifique, para que las ventas sigan
cuadrando, y sale de la libreta. Deshacerla quita la columna (los datos ya
borrados no vuelven).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.drop_column("anonymized_at")
