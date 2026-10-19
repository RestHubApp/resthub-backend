"""alertas a la plataforma por picos de accesos fallidos, bloqueos o 403 (RFS-31).

`platform_security_alerts`: cada pico que pasó su umbral, con cuántas veces
ocurrió, el umbral y la ventana. Deshacerla borra la tabla.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_security_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("window_minutes", sa.Integer(), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_platform_security_alerts_kind_raised",
        "platform_security_alerts",
        ["kind", "raised_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_platform_security_alerts_kind_raised", table_name="platform_security_alerts")
    op.drop_table("platform_security_alerts")
