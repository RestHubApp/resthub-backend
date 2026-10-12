"""consentimiento de tratamiento de datos separado de los términos.

Ley N.º 29733: la cuenta acepta por separado los términos y condiciones
(`terms_version`, `terms_accepted_at`, que ya existían) y el tratamiento de
datos personales (`privacy_version`, `privacy_accepted_at`, nuevas). Las cuentas
existentes quedan sin el segundo consentimiento y se les pide al entrar. Cada
uno tiene su asiento en la bitácora. Deshacerla quita las dos columnas.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("privacy_version", sa.String(20), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("privacy_accepted_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("privacy_accepted_at")
        batch.drop_column("privacy_version")
