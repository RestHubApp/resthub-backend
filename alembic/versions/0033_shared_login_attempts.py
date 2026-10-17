"""intentos de acceso en la base, compartidos entre réplicas (S3b / RFS-02).

`login_attempts` guarda cada intento de acceso de los últimos quince minutos,
con el SHA-256 del correo y de la IP (no el dato en claro), para que el límite
de intentos valga igual con varias réplicas. Se purga sola al anotar intentos
nuevos. Deshacerla borra la tabla: el límite vuelve a contar solo en memoria.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(10), nullable=False),
        sa.Column("email_key", sa.String(64), nullable=False),
        sa.Column("address_key", sa.String(64), nullable=False),
        sa.Column("counts_for_pair", sa.Boolean(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_login_attempts_scope_email_moment",
        "login_attempts",
        ["scope", "email_key", "attempted_at"],
    )
    op.create_index("ix_login_attempts_moment", "login_attempts", ["attempted_at"])


def downgrade() -> None:
    op.drop_index("ix_login_attempts_moment", table_name="login_attempts")
    op.drop_index("ix_login_attempts_scope_email_moment", table_name="login_attempts")
    op.drop_table("login_attempts")
