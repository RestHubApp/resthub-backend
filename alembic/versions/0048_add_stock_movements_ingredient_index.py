"""índice de los movimientos de stock por restaurante e insumo, con la cantidad incluida.

La disponibilidad de la carta y de cada pedido suma el libro por insumo. El libro
solo crece (un consumo por insumo y por plato servido), y el único índice que
empezaba por el restaurante era por fecha. Con `(restaurant_id, ingredient_id)
INCLUDE (quantity)` PostgreSQL resuelve la suma leyendo solo el índice. SQLite no
conoce `INCLUDE` y crea el índice con las dos columnas.

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_stock_movements_restaurant_ingredient"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "stock_movements",
        ["restaurant_id", "ingredient_id"],
        postgresql_include=["quantity"],
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="stock_movements")
