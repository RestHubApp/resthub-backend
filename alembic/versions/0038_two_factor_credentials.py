"""verificación en dos pasos con TOTP (RFS-22).

`two_factor_credentials` guarda, por cuenta del personal o de la plataforma, el
secreto TOTP cifrado, cuándo se confirmó (nulo mientras el alta espera el
primer código), los hashes de los códigos de recuperación que quedan y el
último paso aceptado, para que un código no sirva dos veces. Deshacerla borra
la tabla: las cuentas vuelven a entrar solo con la contraseña.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "two_factor_credentials",
        sa.Column("account_kind", sa.String(10), primary_key=True),
        sa.Column("account_id", sa.Integer(), primary_key=True),
        sa.Column("secret", sa.String(255), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_code_hashes", sa.JSON(), nullable=False),
        sa.Column("last_step", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("two_factor_credentials")
