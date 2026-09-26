"""crear la caja, los pagos y los descuentos.

Un pedido deja de cobrarse con un solo medio de pago: puede pagarse en partes
(cuenta dividida o pago mixto), cada pago cae en un turno de caja y lleva su
propina. También entran el descuento del pedido, las cortesías por plato y el
tope de descuento del mesero en el restaurante.

Los cobros anteriores pasan a `order_payments` como un pago cada uno, con el
medio y el monto recibido que tenía el pedido, sin turno de caja (no existía) y
a nombre del mesero del pedido, que es la mejor aproximación de quién cobró.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(precision=10, scale=2)
_PERCENT = sa.Numeric(precision=5, scale=2)


def upgrade() -> None:
    # Las columnas nuevas de tablas con filas llevan un valor en la base: sin
    # él, una columna obligatoria no entra. El modelo pone el mismo valor al
    # escribir, así que el de la base solo cuenta para las filas viejas.
    with op.batch_alter_table("restaurants") as batch:
        batch.add_column(
            sa.Column(
                "max_waiter_discount_percent", _PERCENT, nullable=False, server_default="10.00"
            )
        )

    op.create_table(
        "cash_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("opened_by", sa.Integer(), nullable=False),
        sa.Column("opening_amount", _MONEY, nullable=False),
        sa.Column("opening_notes", sa.String(length=300), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_by", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counted_cash", _MONEY, nullable=True),
        sa.Column("expected_cash", _MONEY, nullable=True),
        sa.Column("closing_notes", sa.String(length=300), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_cash_sessions_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["opened_by"], ["users.id"], name="fk_cash_sessions_opened_by", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name="fk_cash_sessions_closed_by", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cash_sessions_restaurant_opened",
        "cash_sessions",
        ["restaurant_id", "opened_at"],
        unique=False,
    )
    # Una sola caja abierta por local: índice único parcial, que SQLite y
    # PostgreSQL entienden igual.
    op.create_index(
        "uq_cash_sessions_one_open",
        "cash_sessions",
        ["restaurant_id"],
        unique=True,
        sqlite_where=sa.text("closed_at IS NULL"),
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "order_payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("cash_session_id", sa.Integer(), nullable=True),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("amount", _MONEY, nullable=False),
        sa.Column("tip", _MONEY, nullable=False),
        sa.Column("amount_received", _MONEY, nullable=True),
        sa.Column("received_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_order_payments_restaurant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_order_payments_order", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["cash_session_id"],
            ["cash_sessions.id"],
            name="fk_order_payments_cash_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["received_by"],
            ["users.id"],
            name="fk_order_payments_received_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_payments_order_id", "order_payments", ["order_id"], unique=False)
    op.create_index(
        "ix_order_payments_cash_session_id", "order_payments", ["cash_session_id"], unique=False
    )
    op.create_index(
        "ix_order_payments_restaurant_created",
        "order_payments",
        ["restaurant_id", "created_at"],
        unique=False,
    )

    with op.batch_alter_table("orders") as batch:
        batch.add_column(
            sa.Column("discount_percent", _PERCENT, nullable=False, server_default="0.00")
        )
        batch.add_column(
            sa.Column("discount_reason", sa.String(length=200), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("discounted_by", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("discount_amount", _MONEY, nullable=False, server_default="0.00")
        )
        batch.add_column(
            sa.Column("courtesy_amount", _MONEY, nullable=False, server_default="0.00")
        )
        batch.create_foreign_key(
            "fk_orders_discounted_by", "users", ["discounted_by"], ["id"], ondelete="RESTRICT"
        )

    with op.batch_alter_table("order_items") as batch:
        batch.add_column(
            sa.Column("is_courtesy", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("courtesy_reason", sa.String(length=200), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("payment_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_order_items_payment",
            "order_payments",
            ["payment_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(
        """
        INSERT INTO order_payments (
            restaurant_id, order_id, cash_session_id, method, amount, tip,
            amount_received, received_by, created_at
        )
        SELECT restaurant_id, id, NULL, COALESCE(payment_method, 'cash'), total, 0,
               amount_received, waiter_id, COALESCE(paid_at, updated_at)
        FROM orders
        WHERE status = 'paid'
        """
    )


def downgrade() -> None:
    # El esquema anterior guarda un solo medio por pedido y no conoce los pagos
    # parciales. Un pedido que todavía debe algo perdería lo ya cobrado: mejor
    # no bajar que bajar perdiendo plata.
    pending = op.get_bind().scalar(
        sa.text(
            """
            SELECT COUNT(*) FROM orders o
            WHERE o.status <> 'paid'
              AND EXISTS (SELECT 1 FROM order_payments p WHERE p.order_id = o.id)
            """
        )
    )
    if pending:
        raise RuntimeError(
            f"{pending} pedido(s) tienen pagos parciales sin cerrar. "
            "Cóbralos o cancélalos antes de revertir esta migración."
        )
    # El código anterior no conoce «mixed»: queda el medio del pago mayor.
    op.execute(
        """
        UPDATE orders SET payment_method = (
            SELECT p.method FROM order_payments p
            WHERE p.order_id = orders.id
            ORDER BY p.amount DESC, p.id
            LIMIT 1
        )
        WHERE payment_method = 'mixed'
        """
    )
    with op.batch_alter_table("order_items") as batch:
        batch.drop_constraint("fk_order_items_payment", type_="foreignkey")
        batch.drop_column("payment_id")
        batch.drop_column("courtesy_reason")
        batch.drop_column("is_courtesy")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("fk_orders_discounted_by", type_="foreignkey")
        batch.drop_column("courtesy_amount")
        batch.drop_column("discount_amount")
        batch.drop_column("discounted_by")
        batch.drop_column("discount_reason")
        batch.drop_column("discount_percent")
    op.drop_index("ix_order_payments_restaurant_created", table_name="order_payments")
    op.drop_index("ix_order_payments_cash_session_id", table_name="order_payments")
    op.drop_index("ix_order_payments_order_id", table_name="order_payments")
    op.drop_table("order_payments")
    op.drop_index("uq_cash_sessions_one_open", table_name="cash_sessions")
    op.drop_index("ix_cash_sessions_restaurant_opened", table_name="cash_sessions")
    op.drop_table("cash_sessions")
    with op.batch_alter_table("restaurants") as batch:
        batch.drop_column("max_waiter_discount_percent")
