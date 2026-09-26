"""comprobantes electrónicos: datos fiscales del local, boletas y facturas.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(precision=10, scale=2)
_RATE = sa.Numeric(precision=5, scale=2)


def upgrade() -> None:
    op.create_table(
        "billing_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("ruc", sa.String(length=11), nullable=False),
        sa.Column("legal_name", sa.String(length=100), nullable=False),
        sa.Column("address", sa.String(length=200), nullable=False),
        sa.Column("igv_rate", _RATE, nullable=False),
        sa.Column("boleta_series", sa.String(length=4), nullable=False),
        sa.Column("factura_series", sa.String(length=4), nullable=False),
        sa.Column("provider_url", sa.String(length=300), nullable=False),
        sa.Column("provider_token", sa.String(length=300), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_billing_settings_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("restaurant_id"),
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("series", sa.String(length=4), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("customer_document_type", sa.String(length=10), nullable=False),
        sa.Column("customer_document_number", sa.String(length=15), nullable=False),
        sa.Column("customer_name", sa.String(length=100), nullable=False),
        sa.Column("customer_address", sa.String(length=200), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("total", _MONEY, nullable=False),
        sa.Column("discount", _MONEY, nullable=False),
        sa.Column("igv_rate", _RATE, nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pdf_url", sa.String(length=500), nullable=False),
        sa.Column("provider_message", sa.String(length=300), nullable=False),
        sa.Column("provider_response", sa.JSON(), nullable=False),
        sa.Column("issued_by", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_invoices_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_invoices_order", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"], ["users.id"], name="fk_invoices_issued_by", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("restaurant_id", "series", "number", name="uq_invoices_series_number"),
        sa.UniqueConstraint("restaurant_id", "order_id", name="uq_invoices_order"),
    )
    op.create_index(
        "ix_invoices_restaurant_issued", "invoices", ["restaurant_id", "issued_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_invoices_restaurant_issued", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("billing_settings")
