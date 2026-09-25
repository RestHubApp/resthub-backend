"""anotar cuándo cambia el estado del pedido, aparte de cualquier edición.

`updated_at` se mueve también al editar la nota o el cliente, así que no sirve
para medir cuánto lleva un pedido en cocina o listo. Los pedidos que ya
existían toman su `updated_at`: es la mejor aproximación que queda, y para los
cerrados coincide con el cobro o la cancelación.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Se agrega admitiendo nulos, se rellena y recién entonces se exige: una
    # columna obligatoria sin valor por omisión no entra en una tabla con filas.
    op.add_column(
        "orders", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE orders SET status_changed_at = updated_at")
    # En lote para que SQLite, que no altera columnas, recree la tabla; en
    # PostgreSQL es un `ALTER COLUMN ... SET NOT NULL` común.
    with op.batch_alter_table("orders") as batch:
        batch.alter_column(
            "status_changed_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("status_changed_at")
