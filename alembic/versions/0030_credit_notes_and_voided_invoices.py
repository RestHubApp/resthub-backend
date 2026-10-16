"""notas de crédito y comprobantes anulados: otro comprobante para el mismo pedido.

- `invoices`: la anulación (`voided_at`, `void_reason`, `voided_by`) y el
  comprobante anulado al que reemplaza uno nuevo (`replaces_invoice_id`). El
  único `uq_invoices_order` pasa a ser un índice único parcial: a lo sumo un
  comprobante vigente (sin anular) por pedido.
- `credit_notes`: las notas de crédito que anulan comprobantes aceptados por
  SUNAT, una por comprobante, con su serie y correlativo por local.
- `billing_settings`: las series de esas notas, BC01 y FC01 por omisión.

Deshacerla borra las notas y las columnas nuevas. No se deshace si algún pedido
ya tiene más de un comprobante: el índice antiguo no lo admitiría.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT = "voided_at IS NULL"


def _text(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.Text(), nullable=False, server_default="")


def upgrade() -> None:
    with op.batch_alter_table("billing_settings") as batch:
        batch.add_column(
            sa.Column(
                "boleta_credit_note_series", sa.String(4), nullable=False, server_default="BC01"
            )
        )
        batch.add_column(
            sa.Column(
                "factura_credit_note_series", sa.String(4), nullable=False, server_default="FC01"
            )
        )
    with op.batch_alter_table("invoices") as batch:
        batch.drop_constraint("uq_invoices_order", type_="unique")
        batch.add_column(sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("void_reason", sa.String(200), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("voided_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("replaces_invoice_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_invoices_voided_by", "users", ["voided_by"], ["id"], ondelete="RESTRICT"
        )
        batch.create_foreign_key(
            "fk_invoices_replaces", "invoices", ["replaces_invoice_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_index(
        "uq_invoices_order_current",
        "invoices",
        ["restaurant_id", "order_id"],
        unique=True,
        sqlite_where=sa.text(_CURRENT),
        postgresql_where=sa.text(_CURRENT),
    )
    op.create_table(
        "credit_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("series", sa.String(4), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(2), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        _text("signed_xml"),
        _text("cdr_zip"),
        _text("document_payload"),
        sa.Column("provider_message", sa.String(300), nullable=False),
        sa.Column("provider_response", sa.JSON(), nullable=False),
        sa.Column("issued_by", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_credit_notes_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], name="fk_credit_notes_invoice", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"], ["users.id"], name="fk_credit_notes_issued_by", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id"),
        sa.UniqueConstraint(
            "restaurant_id", "series", "number", name="uq_credit_notes_series_number"
        ),
    )


def downgrade() -> None:
    repeated = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM (SELECT order_id FROM invoices "
                "GROUP BY restaurant_id, order_id HAVING COUNT(*) > 1) AS repeated"
            )
        )
        .scalar_one()
    )
    if repeated:
        message = (
            f"{repeated} pedidos tienen más de un comprobante (uno anulado y su reemplazo): "
            "la versión anterior admite uno solo por pedido."
        )
        raise RuntimeError(message)
    op.drop_table("credit_notes")
    op.drop_index("uq_invoices_order_current", table_name="invoices")
    with op.batch_alter_table("invoices") as batch:
        batch.drop_constraint("fk_invoices_replaces", type_="foreignkey")
        batch.drop_constraint("fk_invoices_voided_by", type_="foreignkey")
        batch.drop_column("replaces_invoice_id")
        batch.drop_column("voided_by")
        batch.drop_column("void_reason")
        batch.drop_column("voided_at")
        batch.create_unique_constraint("uq_invoices_order", ["restaurant_id", "order_id"])
    with op.batch_alter_table("billing_settings") as batch:
        batch.drop_column("factura_credit_note_series")
        batch.drop_column("boleta_credit_note_series")
