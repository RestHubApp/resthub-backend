"""historial de inicios de sesión (RFS-15, RFS-16).

`access_history` guarda cada intento de acceso: cuándo, cómo terminó, el
dispositivo (navegador y sistema), la IP sin su último bloque, la ciudad y el
país (con una base GeoLite local), y si es la primera vez que la cuenta entra
desde ese dispositivo o ese país. La cuenta y el restaurante van como enteros
sueltos, sin clave foránea: la fila es de `users` o de `platform_admins` según
`account_kind`. Se purga a los 90 días. Deshacerla borra la tabla.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_kind", sa.String(10), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("restaurant_id", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(12), nullable=False),
        sa.Column("device", sa.String(120), nullable=False),
        sa.Column("network", sa.String(48), nullable=False),
        sa.Column("city", sa.String(80), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("new_device", sa.Boolean(), nullable=False),
        sa.Column("new_country", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "ix_access_history_account_moment",
        "access_history",
        ["account_kind", "account_id", "occurred_at"],
    )
    op.create_index(
        "ix_access_history_restaurant_moment", "access_history", ["restaurant_id", "occurred_at"]
    )
    op.create_index("ix_access_history_moment", "access_history", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_access_history_moment", table_name="access_history")
    op.drop_index("ix_access_history_restaurant_moment", table_name="access_history")
    op.drop_index("ix_access_history_account_moment", table_name="access_history")
    op.drop_table("access_history")
