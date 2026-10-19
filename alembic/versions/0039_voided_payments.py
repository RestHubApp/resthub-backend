"""cobros anulados: quién, cuándo y por qué (RFS-25).

`order_payments.voided_at`, `voided_by` y `void_reason`. Un cobro anulado deja
de sumar a la cuenta, a la caja y a los reportes, pero no se borra. Deshacerla
quita las tres columnas.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("order_payments") as batch:
        batch.add_column(sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("voided_by", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("void_reason", sa.String(200), nullable=False, server_default="")
        )
        batch.create_foreign_key(
            "fk_order_payments_voided_by", "users", ["voided_by"], ["id"], ondelete="RESTRICT"
        )


def downgrade() -> None:
    with op.batch_alter_table("order_payments") as batch:
        batch.drop_constraint("fk_order_payments_voided_by", type_="foreignkey")
        batch.drop_column("void_reason")
        batch.drop_column("voided_by")
        batch.drop_column("voided_at")
