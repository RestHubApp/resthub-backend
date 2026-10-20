"""secreto TOTP como `EncryptedText` y contraseñas temporales que vencen.

- `two_factor_credentials.secret` ya guardaba `enc1:<llave>:<...>`, el mismo
  formato de `EncryptedText` (lo cifraba a mano `core/two_factor.py`), así que los
  valores no cambian: solo el tipo de la columna pasa de `VARCHAR(255)` a `TEXT`,
  como las demás columnas cifradas, y con eso entra en la rotación de la llave
  (`scripts/rotate_data_key.py`, ASVS 11.2.2).
- `users.temporary_password_set_at`: cuándo se fijó la contraseña temporal; vence a
  las `TEMPORARY_PASSWORD_HOURS` (ASVS 6.4.1). Las cuentas que hoy tienen una
  temporal empiezan a contar desde la migración, no desde su alta: si no, vencerían
  todas de golpe.
- `platform_admins.must_change_password` y `temporary_password_set_at`: la
  contraseña que genera `scripts/create_platform_admin.py` es temporal, como la del
  personal (ASVS 6.2.2). Las cuentas que ya existen quedan sin temporal.

Deshacerla quita las columnas y devuelve el `VARCHAR(255)` (un secreto cifrado
ocupa unos 110 caracteres).

Revision ID: 0046
Revises: 0043
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("two_factor_credentials") as batch:
        batch.alter_column(
            "secret", existing_type=sa.String(length=255), type_=sa.Text(), existing_nullable=False
        )
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("temporary_password_set_at", sa.DateTime(timezone=True), nullable=True)
        )
    users = sa.table(
        "users",
        sa.column("must_change_password", sa.Boolean),
        sa.column("temporary_password_set_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        users.update()
        .where(users.c.must_change_password.is_(True))
        .values(temporary_password_set_at=sa.func.current_timestamp())
    )
    with op.batch_alter_table("platform_admins") as batch:
        batch.add_column(
            sa.Column(
                "must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(
            sa.Column("temporary_password_set_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("platform_admins") as batch:
        batch.drop_column("temporary_password_set_at")
        batch.drop_column("must_change_password")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("temporary_password_set_at")
    with op.batch_alter_table("two_factor_credentials") as batch:
        batch.alter_column(
            "secret", existing_type=sa.Text(), type_=sa.String(length=255), existing_nullable=False
        )
