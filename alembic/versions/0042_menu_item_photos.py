"""fotos de los platos del menú.

`menu_items.photo_version`: cuándo se subió la foto vigente (milisegundos desde
1970); NULL, sin foto. `menu_item_photos`: la imagen en sí, aparte para que leer
la carta no la arrastre. Deshacerla quita la columna y la tabla (se pierden las
fotos).

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("menu_items") as batch:
        batch.add_column(sa.Column("photo_version", sa.BigInteger(), nullable=True))
    op.create_table(
        "menu_item_photos",
        sa.Column("menu_item_id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=20), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["menu_item_id"],
            ["menu_items.id"],
            name="fk_menu_item_photos_item",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_menu_item_photos_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("menu_item_id"),
    )
    op.create_index(
        op.f("ix_menu_item_photos_restaurant_id"), "menu_item_photos", ["restaurant_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_menu_item_photos_restaurant_id"), table_name="menu_item_photos")
    op.drop_table("menu_item_photos")
    with op.batch_alter_table("menu_items") as batch:
        batch.drop_column("photo_version")
