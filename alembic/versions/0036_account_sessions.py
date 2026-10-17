"""sesiones por dispositivo (RFS-17, RFS-21, RFS-29).

`account_sessions` guarda una fila por sesión abierta: de qué cuenta es, desde
qué dispositivo y red se abrió, cuándo se usó por última vez y si se cerró. El
token lleva su identificador firmado (`sid`). La misma fila dice cuándo se
volvió a confirmar la contraseña en ese dispositivo y si está bloqueada por
inactividad. Los tokens emitidos antes no traen `sid` y dejan de valer: cada
cuenta vuelve a entrar una vez. Deshacerla borra la tabla.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_sessions",
        sa.Column("id", sa.String(43), primary_key=True),
        sa.Column("account_kind", sa.String(10), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device", sa.String(120), nullable=False),
        sa.Column("network", sa.String(48), nullable=False),
        sa.Column("city", sa.String(80), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reauthenticated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unlock_failures", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_account_sessions_account",
        "account_sessions",
        ["account_kind", "account_id", "last_seen_at"],
    )
    op.create_index("ix_account_sessions_last_seen", "account_sessions", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_account_sessions_last_seen", table_name="account_sessions")
    op.drop_index("ix_account_sessions_account", table_name="account_sessions")
    op.drop_table("account_sessions")
