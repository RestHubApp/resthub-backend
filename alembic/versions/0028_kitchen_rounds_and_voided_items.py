"""rondas de cocina y platos anulados.

`orders.kitchen_round` es la ronda vigente en cocina (1 al enviar el pedido, una
más por cada tanda de platos agregada después) y `order_items.kitchen_round`, la
ronda en que llegó cada plato. Con ellas la cocina distingue los platos nuevos.

`order_items.voided_at` y `void_reason` anotan un plato anulado después de
enviarlo: no se cobra, pero se conserva para la historia.

Los pedidos que ya salieron de `open` (y sus platos) quedan en la ronda 1: no
hay forma de saber en qué tanda llegó cada plato. Deshacerla borra las columnas.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _round() -> sa.Column[int]:
    return sa.Column("kitchen_round", sa.Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(_round())
    with op.batch_alter_table("order_items") as batch:
        batch.add_column(_round())
        batch.add_column(sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("void_reason", sa.String(length=200), nullable=False, server_default="")
        )
    op.execute("UPDATE orders SET kitchen_round = 1 WHERE status <> 'open'")
    op.execute(
        "UPDATE order_items SET kitchen_round = 1 "
        "WHERE order_id IN (SELECT id FROM orders WHERE status <> 'open')"
    )


def downgrade() -> None:
    with op.batch_alter_table("order_items") as batch:
        batch.drop_column("void_reason")
        batch.drop_column("voided_at")
        batch.drop_column("kitchen_round")
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("kitchen_round")
