"""crear las decisiones de IA: qué se preguntó, qué se respondió y quién.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        # Sin clave foránea: según `subject_type` apunta a un insumo, un pedido,
        # un plato de un pedido o un movimiento de stock.
        sa.Column("subject_type", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("input_state", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("engine", sa.String(length=10), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_ai_decisions_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_decisions_subject",
        "ai_decisions",
        ["restaurant_id", "kind", "subject_type", "subject_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_decisions_restaurant_moment",
        "ai_decisions",
        ["restaurant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_decisions_restaurant_moment", table_name="ai_decisions")
    op.drop_index("ix_ai_decisions_subject", table_name="ai_decisions")
    op.drop_table("ai_decisions")
