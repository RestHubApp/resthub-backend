"""clientes: un teléfono solo puede estar en una ficha del local.

`SaveCustomer` revisaba el teléfono y después insertaba, sin índice único: dos
`POST /customers` a la vez (un doble toque, o el reintento del pedido para llevar
que se corta a los 4 s) creaban dos fichas con el mismo teléfono, y después ninguna
se podía editar sin cambiarlo. Se cambia `ix_customers_restaurant_phone` por
`uq_customers_restaurant_phone`, único y parcial sobre los teléfonos no vacíos (los
clientes sin teléfono y los borrados tienen `phone_key` vacío).

Antes, si ya hay duplicados, se queda la ficha más antigua con el teléfono buscable
y a las demás se les vacía `phone_key`: conservan su teléfono cifrado y sus pedidos,
pero ya no se encuentran por teléfono. Deshacerla vuelve al índice común; los
`phone_key` vaciados no se recuperan.

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_INDEX = "ix_customers_restaurant_phone"
INDEX = "uq_customers_restaurant_phone"
COLUMNS = ["restaurant_id", "phone_key"]
NOT_EMPTY = sa.text("phone_key <> ''")


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE customers SET phone_key = '' "
            "WHERE phone_key <> '' AND EXISTS ("
            "SELECT 1 FROM customers AS kept "
            "WHERE kept.restaurant_id = customers.restaurant_id "
            "AND kept.phone_key = customers.phone_key AND kept.id < customers.id)"
        )
    )
    with op.batch_alter_table("customers") as batch:
        batch.drop_index(OLD_INDEX)
        batch.create_index(
            INDEX, COLUMNS, unique=True, postgresql_where=NOT_EMPTY, sqlite_where=NOT_EMPTY
        )


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.drop_index(INDEX)
        batch.create_index(OLD_INDEX, COLUMNS, unique=False)
