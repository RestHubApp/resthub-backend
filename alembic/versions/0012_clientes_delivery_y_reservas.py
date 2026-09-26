"""clientes frecuentes, delivery, reservas y pedidos que no se duplican.

- `customers`: la libreta de clientes del local.
- `orders`: datos de entrega (teléfono, dirección, referencia), el cliente de
  la libreta y el identificador que genera el celular del mesero, para que un
  reintento sin señal no cree el pedido dos veces.
- `reservations`: reservas de mesa.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("phone_key", sa.String(length=20), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("address", sa.String(length=200), nullable=False),
        sa.Column("reference", sa.String(length=150), nullable=False),
        sa.Column("notes", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_customers_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_customers_restaurant_phone", "customers", ["restaurant_id", "phone_key"], unique=False
    )
    op.create_index(
        "ix_customers_restaurant_name", "customers", ["restaurant_id", "name"], unique=False
    )

    with op.batch_alter_table("orders") as batch:
        batch.add_column(
            sa.Column("customer_phone", sa.String(length=20), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("delivery_address", sa.String(length=200), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column(
                "delivery_reference", sa.String(length=150), nullable=False, server_default=""
            )
        )
        batch.add_column(sa.Column("customer_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("client_request_id", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_orders_customer", "customers", ["customer_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_orders_customer_id", ["customer_id"], unique=False)
        batch.create_unique_constraint(
            "uq_orders_client_request", ["restaurant_id", "client_request_id"]
        )

    op.create_table(
        "reservations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("customer_name", sa.String(length=80), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("reserved_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("table_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_reservations_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"], ["dining_tables.id"], name="fk_reservations_table", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name="fk_reservations_customer", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_reservations_created_by", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reservations_restaurant_time",
        "reservations",
        ["restaurant_id", "reserved_for"],
        unique=False,
    )
    op.create_index(
        "ix_reservations_table_time", "reservations", ["table_id", "reserved_for"], unique=False
    )


def downgrade() -> None:
    # El código anterior solo conoce `dine_in` y `takeaway`. Un delivery es,
    # para él, un pedido para llevar: se conserva la venta y se pierde solo la
    # dirección de entrega, que esta reversión borra de todos modos.
    op.execute("UPDATE orders SET type = 'takeaway' WHERE type = 'delivery'")
    op.drop_index("ix_reservations_table_time", table_name="reservations")
    op.drop_index("ix_reservations_restaurant_time", table_name="reservations")
    op.drop_table("reservations")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("uq_orders_client_request", type_="unique")
        batch.drop_index("ix_orders_customer_id")
        batch.drop_constraint("fk_orders_customer", type_="foreignkey")
        batch.drop_column("client_request_id")
        batch.drop_column("customer_id")
        batch.drop_column("delivery_reference")
        batch.drop_column("delivery_address")
        batch.drop_column("customer_phone")
    op.drop_index("ix_customers_restaurant_name", table_name="customers")
    op.drop_index("ix_customers_restaurant_phone", table_name="customers")
    op.drop_table("customers")
