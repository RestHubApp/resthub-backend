"""aceptación de los términos y la política de privacidad.

Ley N.º 29733: cada cuenta acepta los términos de uso y la política de
privacidad vigentes antes de trabajar. `users.terms_version` guarda la última
versión aceptada y `terms_accepted_at`, cuándo; el historial completo queda en
la bitácora (`terms_accepted`). Las cuentas existentes quedan sin aceptar y se
les piden al entrar. Deshacerla quita las dos columnas.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("terms_version", sa.String(20), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("terms_accepted_at")
        batch.drop_column("terms_version")
