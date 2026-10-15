"""facturación por Lycet: credenciales SOL y certificado por local, documentos guardados.

Lycet (Greenter) reemplaza a Nubefact. En `billing_settings` se van la URL y el
token del proveedor (Lycet es uno para todo el servidor: `LYCET_URL` y
`LYCET_TOKEN`) y llegan el usuario SOL, la clave SOL y el certificado digital en
PEM, cifrados en reposo como los datos personales (`core/crypto.py`), más la
fecha de vencimiento del certificado.

En `invoices` se van los enlaces al PDF, el XML y el CDR (Lycet no da enlaces) y
llegan, cifrados, el XML firmado, el CDR (ZIP en base64) y el documento enviado,
con el que se vuelve a armar el PDF. Los enlaces de Nubefact se pierden: un
comprobante antiguo se puede reenviar para tener sus documentos. Los tokens de
Nubefact se descartan.

Deshacerla devuelve las columnas antiguas vacías y descarta lo nuevo.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _text(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.Text(), nullable=False, server_default="")


def upgrade() -> None:
    with op.batch_alter_table("billing_settings") as batch:
        batch.drop_column("provider_token")
        batch.drop_column("provider_url")
        batch.add_column(_text("sol_user"))
        batch.add_column(_text("sol_password"))
        batch.add_column(_text("certificate_pem"))
        batch.add_column(
            sa.Column("certificate_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("cdr_url")
        batch.drop_column("xml_url")
        batch.drop_column("pdf_url")
        batch.add_column(_text("signed_xml"))
        batch.add_column(_text("cdr_zip"))
        batch.add_column(_text("document_payload"))


def downgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("document_payload")
        batch.drop_column("cdr_zip")
        batch.drop_column("signed_xml")
        batch.add_column(sa.Column("pdf_url", sa.String(500), nullable=False, server_default=""))
        batch.add_column(sa.Column("xml_url", sa.String(500), nullable=False, server_default=""))
        batch.add_column(sa.Column("cdr_url", sa.String(500), nullable=False, server_default=""))
    with op.batch_alter_table("billing_settings") as batch:
        batch.drop_column("certificate_expires_at")
        batch.drop_column("certificate_pem")
        batch.drop_column("sol_password")
        batch.drop_column("sol_user")
        batch.add_column(
            sa.Column("provider_url", sa.String(300), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("provider_token", sa.String(300), nullable=False, server_default="")
        )
