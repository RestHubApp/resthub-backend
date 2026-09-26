"""opciones de los platos y platos agotados por falta de insumos.

La carta guarda los grupos de opciones de cada plato y cada ítem lo que se
eligió. El restaurante decide si un plato sin insumos para una porción se
agota solo (`auto_out_of_stock`, activado por omisión).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("restaurants") as batch:
        batch.add_column(
            sa.Column("auto_out_of_stock", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    # Las filas que ya existían arrancan sin opciones: una lista vacía.
    with op.batch_alter_table("menu_items") as batch:
        batch.add_column(
            sa.Column("modifier_groups", sa.JSON(), nullable=False, server_default="[]")
        )
    with op.batch_alter_table("order_items") as batch:
        batch.add_column(sa.Column("modifiers", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("order_items") as batch:
        batch.drop_column("modifiers")
    with op.batch_alter_table("menu_items") as batch:
        batch.drop_column("modifier_groups")
    with op.batch_alter_table("restaurants") as batch:
        batch.drop_column("auto_out_of_stock")
