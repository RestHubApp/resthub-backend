"""consentimiento de los clientes para tratar sus datos.

Ley N.º 29733: el alta de un cliente en la libreta exige su consentimiento
informado. Se anota cuándo lo dio (`consent_at`), sobre qué versión del texto
(`consent_version`) y qué cuenta lo registró (`consent_by`). Los clientes ya
guardados quedan sin consentimiento (`consent_at` nulo) hasta que se registre.
Deshacerla quita las tres columnas.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("consent_version", sa.String(20), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("consent_by", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_customers_consent_by", "users", ["consent_by"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.drop_constraint("fk_customers_consent_by", type_="foreignkey")
        batch.drop_column("consent_by")
        batch.drop_column("consent_version")
        batch.drop_column("consent_at")
