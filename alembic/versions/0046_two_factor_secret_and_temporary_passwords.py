"""secreto TOTP como `EncryptedText`.

`two_factor_credentials.secret` ya guardaba `enc1:<llave>:<...>`, el mismo
formato de `EncryptedText` (lo cifraba a mano `core/two_factor.py`), así que los
valores no cambian: solo el tipo de la columna pasa de `VARCHAR(255)` a `TEXT`,
como las demás columnas cifradas, y con eso entra en la rotación de la llave
(`scripts/rotate_data_key.py`). Deshacerla devuelve el `VARCHAR(255)`: un
secreto cifrado ocupa unos 110 caracteres.

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


def downgrade() -> None:
    with op.batch_alter_table("two_factor_credentials") as batch:
        batch.alter_column(
            "secret", existing_type=sa.Text(), type_=sa.String(length=255), existing_nullable=False
        )
