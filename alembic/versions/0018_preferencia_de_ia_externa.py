"""preferencia del local sobre la IA externa.

Las notas de los pedidos y los motivos de merma se envían, sin datos
personales, a un proveedor de IA fuera del Perú (Ley N.º 29733, flujo
transfronterizo). `restaurants.external_ai_enabled` deja al local apagarlo:
entonces deciden las reglas y nada sale. Arranca encendida, como hasta ahora.
Deshacerla quita la columna.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("restaurants") as batch:
        batch.add_column(
            sa.Column("external_ai_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("restaurants") as batch:
        batch.drop_column("external_ai_enabled")
