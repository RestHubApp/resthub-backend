"""cifrar el correo del personal, las notas de los platos y los datos fiscales (Ley N.º 29733).

Pasan a `EncryptedText` (AES-256-GCM, `core/crypto.py`), como los datos del cliente en
la `0026`:

- `users.email`. El acceso busca la cuenta por correo, así que se agrega
  `users.email_key`, su índice ciego (HMAC-SHA256 del correo en minúsculas), y la
  restricción única pasa del correo a ese índice;
- `users.deactivation_reason`, texto libre que puede traer algo delicado;
- `order_items.notes`, que puede traer una alergia (dato de salud);
- `billing_settings.ruc` y `billing_settings.address`: si el dueño es persona
  natural, su RUC lleva su DNI y la dirección fiscal puede ser su casa.

Las filas existentes se cifran aquí con la `DATA_ENCRYPTION_KEY` vigente. Deshacerla
descifra todo y devuelve la restricción única al correo: hace falta la misma llave.
Es válida en SQLite y en PostgreSQL.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from resthub.core.crypto import get_cipher

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 500

# tabla -> columna cifrada y el largo que tenía antes.
ENCRYPTED: dict[str, dict[str, int]] = {
    "users": {"email": 254, "deactivation_reason": 200},
    "order_items": {"notes": 200},
    "billing_settings": {"ruc": 11, "address": 200},
}
INDEX_LENGTH = 64


def _rewrite(table: str, *, encrypt: bool) -> None:
    """Cifra (o descifra) cada valor de la tabla, por lotes de ids."""
    connection = op.get_bind()
    cipher = get_cipher()
    columns = list(ENCRYPTED[table])
    is_users = table == "users"
    names = [*columns, "email_key"] if is_users and encrypt else columns
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
            if is_users and encrypt:
                changes["email_key"] = cipher.blind_index(plain["email"].strip().lower())
            connection.execute(sa.update(target).where(target.c.id == row.id).values(**changes))
        last_id = batch[-1].id


def _retype(table: str, *, to_text: bool) -> None:
    with op.batch_alter_table(table) as batch:
        for column, length in ENCRYPTED[table].items():
            batch.alter_column(
                column,
                existing_type=sa.String(length) if to_text else sa.Text(),
                type_=sa.Text() if to_text else sa.String(length),
                existing_nullable=False,
            )


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_email")
        batch.add_column(sa.Column("email_key", sa.String(INDEX_LENGTH), nullable=True))
    for table in ENCRYPTED:
        _retype(table, to_text=True)
        _rewrite(table, encrypt=True)
    with op.batch_alter_table("users") as batch:
        batch.alter_column("email_key", existing_type=sa.String(INDEX_LENGTH), nullable=False)
        batch.create_index("ix_users_email_key", ["email_key"], unique=True)


def downgrade() -> None:
    for table in ENCRYPTED:
        _rewrite(table, encrypt=False)
        _retype(table, to_text=False)
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_email_key")
        batch.drop_column("email_key")
        batch.create_index("ix_users_email", ["email"], unique=True)
