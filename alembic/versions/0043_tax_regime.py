"""régimen tributario del local.

`billing_settings.tax_regime`: `general`, `special`, `mype` o `new_rus`. Decide
qué comprobantes emite el local (el Nuevo RUS solo boletas). Los locales que ya
existían quedan en `general`, como emitían hasta ahora. Deshacerla quita la
columna.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("billing_settings") as batch:
        batch.add_column(
            sa.Column("tax_regime", sa.String(length=10), nullable=False, server_default="general")
        )


def downgrade() -> None:
    with op.batch_alter_table("billing_settings") as batch:
        batch.drop_column("tax_regime")
