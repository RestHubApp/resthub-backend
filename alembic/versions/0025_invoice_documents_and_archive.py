"""enlaces al XML y al CDR de cada comprobante y estado de su respaldo en Drive.

Agrega a `invoices` los enlaces al XML y al CDR que devuelve el proveedor y el
estado del respaldo (`disabled`, `pending`, `done` o `failed`), su mensaje y la
fecha en que se completó. Los comprobantes ya emitidos quedan en `disabled` y
sin enlaces al XML: solo los nuevos se respaldan; uno antiguo se puede reenviar
al proveedor y pedir su respaldo desde la pantalla de comprobantes. Deshacerla
quita las columnas.

Revision ID: 0025
Revises: 0023
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        batch.add_column(sa.Column("xml_url", sa.String(500), nullable=False, server_default=""))
        batch.add_column(sa.Column("cdr_url", sa.String(500), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("archive_status", sa.String(20), nullable=False, server_default="disabled")
        )
        batch.add_column(
            sa.Column("archive_message", sa.String(300), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("archived_at")
        batch.drop_column("archive_message")
        batch.drop_column("archive_status")
        batch.drop_column("cdr_url")
        batch.drop_column("xml_url")
