"""cifrar en reposo los datos personales de clientes (Ley N.º 29733).

Teléfono, correo, dirección, referencia y notas de `customers`; lo mismo que se
copia en `orders` (teléfono, dirección, referencia y notas), `reservations`
(teléfono y notas) y `invoices` (documento y dirección) pasa a `Text` con
AES-256-GCM (`core/crypto.py`). `customers.phone_key` deja de ser el teléfono
sin espacios y pasa a ser su índice ciego (HMAC-SHA256). Las filas existentes
se cifran aquí, con la `DATA_ENCRYPTION_KEY` vigente; sin llave en producción la
migración falla antes de tocar nada.

Deshacerla descifra todo y devuelve el teléfono sin espacios a `phone_key`: hace
falta la misma llave. Es válida en SQLite y en PostgreSQL.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from resthub.core.crypto import get_cipher

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 500

# tabla -> columna cifrada y el largo que tenía antes.
ENCRYPTED: dict[str, dict[str, int]] = {
    "customers": {"phone": 20, "email": 120, "address": 200, "reference": 150, "notes": 300},
    "orders": {
        "customer_phone": 20,
        "delivery_address": 200,
        "delivery_reference": 150,
        "notes": 300,
    },
    "reservations": {"phone": 20, "notes": 300},
    "invoices": {"customer_document_number": 15, "customer_address": 200},
}
PHONE_KEY_LENGTH = 20
INDEX_LENGTH = 64


def _rewrite(table: str, *, encrypt: bool) -> None:
    """Cifra (o descifra) cada valor de la tabla, por lotes de ids."""
    connection = op.get_bind()
    cipher = get_cipher()
    columns = list(ENCRYPTED[table])
    is_customers = table == "customers"
    names = [*columns, "phone_key"] if is_customers else columns
    target = sa.table(table, sa.column("id", sa.Integer), *(sa.column(c, sa.Text) for c in names))
    last_id = 0
    while True:
        batch = connection.execute(
            sa.select(target).where(target.c.id > last_id).order_by(target.c.id).limit(BATCH)
        ).all()
        if not batch:
            return
        for row in batch:
            plain = {c: cipher.decrypt(getattr(row, c) or "") for c in columns}
            changes = {c: cipher.encrypt(v) for c, v in plain.items()} if encrypt else plain
            if is_customers:
                key = plain["phone"].replace(" ", "")
                changes["phone_key"] = cipher.blind_index(key) if encrypt else key
            connection.execute(sa.update(target).where(target.c.id == row.id).values(**changes))
        last_id = batch[-1].id


def upgrade() -> None:
    for table, columns in ENCRYPTED.items():
        with op.batch_alter_table(table) as batch:
            for column, length in columns.items():
                batch.alter_column(
                    column,
                    existing_type=sa.String(length),
                    type_=sa.Text(),
                    existing_nullable=False,
                )
            if table == "customers":
                batch.alter_column(
                    "phone_key",
                    existing_type=sa.String(PHONE_KEY_LENGTH),
                    type_=sa.String(INDEX_LENGTH),
                    existing_nullable=False,
                )
    for table in ENCRYPTED:
        _rewrite(table, encrypt=True)


def downgrade() -> None:
    for table in ENCRYPTED:
        _rewrite(table, encrypt=False)
    for table, columns in ENCRYPTED.items():
        with op.batch_alter_table(table) as batch:
            for column, length in columns.items():
                batch.alter_column(
                    column,
                    existing_type=sa.Text(),
                    type_=sa.String(length),
                    existing_nullable=False,
                )
            if table == "customers":
                batch.alter_column(
                    "phone_key",
                    existing_type=sa.String(INDEX_LENGTH),
                    type_=sa.String(PHONE_KEY_LENGTH),
                    existing_nullable=False,
                )
