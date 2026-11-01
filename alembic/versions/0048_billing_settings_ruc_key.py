"""datos fiscales: un RUC solo puede estar en un local.

Lycet es uno para todo el servidor y guarda el usuario SOL y el certificado por RUC.
Si otro local escribía el mismo RUC (un RUC es público: sale en cada boleta), sus
credenciales pisaban las del primero, y al cambiarlo después se borraba la empresa del
primero en Lycet. `billing_settings.ruc` está cifrado (`0047`), así que se agrega
`billing_settings.ruc_key`, su índice ciego (HMAC-SHA256 del RUC), con un índice único
parcial sobre los que no están vacíos. El local de muestra no toma ninguno: nunca
registra nada en Lycet.

Si dos locales reales ya comparten un RUC, la migración se detiene y dice cuáles: lo
resuelve una persona antes de volver a correrla. Deshacerla quita el índice y la
columna. Es válida en SQLite y en PostgreSQL.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from resthub.core.crypto import get_cipher

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_billing_settings_ruc_key"
INDEX_LENGTH = 64
NOT_EMPTY = sa.text("ruc_key <> ''")


def _backfill() -> None:
    connection = op.get_bind()
    cipher = get_cipher()
    settings = sa.table(
        "billing_settings",
        sa.column("id", sa.Integer),
        sa.column("restaurant_id", sa.Integer),
        sa.column("ruc", sa.Text),
        sa.column("ruc_key", sa.String),
    )
    restaurants = sa.table("restaurants", sa.column("id", sa.Integer), sa.column("is_sandbox"))
    rows = connection.execute(
        sa.select(settings.c.id, settings.c.restaurant_id, settings.c.ruc)
        .join(restaurants, restaurants.c.id == settings.c.restaurant_id)
        .where(restaurants.c.is_sandbox == sa.false())
        .order_by(settings.c.id)
    ).all()
    owners: dict[str, list[int]] = defaultdict(list)
    keys: dict[int, str] = {}
    for row in rows:
        ruc = cipher.decrypt(row.ruc or "").strip()
        if ruc:
            key = cipher.blind_index(ruc)
            owners[key].append(int(row.restaurant_id))
            keys[int(row.id)] = key
    shared = [ids for ids in owners.values() if len(ids) > 1]
    if shared:
        message = (
            "Hay locales que comparten un RUC en billing_settings (restaurant_id): "
            f"{shared}. Deja el RUC en uno solo y vuelve a correr la migración."
        )
        raise RuntimeError(message)
    for row_id, key in keys.items():
        connection.execute(sa.update(settings).where(settings.c.id == row_id).values(ruc_key=key))


def upgrade() -> None:
    with op.batch_alter_table("billing_settings") as batch:
        batch.add_column(
            sa.Column("ruc_key", sa.String(INDEX_LENGTH), nullable=False, server_default="")
        )
    _backfill()
    with op.batch_alter_table("billing_settings") as batch:
        batch.create_index(
            INDEX,
            ["ruc_key"],
            unique=True,
            postgresql_where=NOT_EMPTY,
            sqlite_where=NOT_EMPTY,
        )


def downgrade() -> None:
    with op.batch_alter_table("billing_settings") as batch:
        batch.drop_index(INDEX)
        batch.drop_column("ruc_key")
