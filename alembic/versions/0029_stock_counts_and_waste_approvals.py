"""conteos físicos, presentaciones de insumos y mermas por aprobar.

- `ingredients`: la presentación (`package_size`, `package_label`) y los pesos
  del envase lleno y vacío para contar por peso. Todo opcional.
- `stock_movements`: cómo se ingresó la cantidad (`entry_mode`,
  `entry_quantity`) y el conteo que originó un ajuste (`stock_count_id`).
- `stock_counts` y `stock_count_lines`: los conteos físicos. Un índice parcial
  deja un solo conteo en curso por local.
- `waste_approvals`: mermas que superan el monto del local y esperan al
  encargado; no mueven el stock hasta aprobarse.
- `restaurants.waste_approval_threshold`: ese monto, en soles. Los locales que
  ya existen quedan con S/ 50.00, el valor por omisión.

Deshacerla borra las tablas y columnas nuevas, con lo que tengan.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quantity(name: str, *, nullable: bool = True) -> sa.Column[object]:
    return sa.Column(name, sa.Numeric(precision=12, scale=3), nullable=nullable)


def _restaurant(table: str) -> sa.Column[int]:
    return sa.Column(
        "restaurant_id",
        sa.Integer(),
        sa.ForeignKey("restaurants.id", name=f"fk_{table}_restaurant", ondelete="RESTRICT"),
        nullable=False,
    )


def _user(column: str, name: str, *, nullable: bool) -> sa.Column[int]:
    return sa.Column(
        column,
        sa.Integer(),
        sa.ForeignKey("users.id", name=name, ondelete="RESTRICT"),
        nullable=nullable,
    )


def upgrade() -> None:
    with op.batch_alter_table("restaurants") as batch:
        batch.add_column(
            sa.Column(
                "waste_approval_threshold",
                sa.Numeric(precision=10, scale=2),
                nullable=True,
                server_default="50.00",
            )
        )
    with op.batch_alter_table("ingredients") as batch:
        batch.add_column(_quantity("package_size"))
        batch.add_column(
            sa.Column("package_label", sa.String(length=30), nullable=False, server_default="")
        )
        batch.add_column(_quantity("full_weight_g"))
        batch.add_column(_quantity("empty_weight_g"))

    op.create_table(
        "stock_counts",
        sa.Column("id", sa.Integer(), primary_key=True),
        _restaurant("stock_counts"),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=False),
        _user("created_by", "fk_stock_counts_created_by", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _user("closed_by", "fk_stock_counts_closed_by", nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("restaurant_id", "number", name="uq_stock_counts_number"),
    )
    op.create_index(
        "uq_stock_counts_one_draft",
        "stock_counts",
        ["restaurant_id"],
        unique=True,
        sqlite_where=sa.text("status = 'draft'"),
        postgresql_where=sa.text("status = 'draft'"),
    )

    op.create_table(
        "stock_count_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        _restaurant("stock_count_lines"),
        sa.Column(
            "stock_count_id",
            sa.Integer(),
            sa.ForeignKey("stock_counts.id", name="fk_stock_count_lines_count", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ingredient_id",
            sa.Integer(),
            sa.ForeignKey(
                "ingredients.id", name="fk_stock_count_lines_ingredient", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        _quantity("opening_stock", nullable=False),
        _quantity("expected", nullable=False),
        _quantity("counted"),
        sa.Column("entry_mode", sa.String(length=10), nullable=True),
        _quantity("entry_quantity"),
        _quantity("entry_full_packages"),
        _user("counted_by", "fk_stock_count_lines_counted_by", nullable=True),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=True),
        _quantity("difference"),
        sa.Column("unit_cost", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.UniqueConstraint(
            "stock_count_id", "ingredient_id", name="uq_stock_count_lines_ingredient"
        ),
    )
    op.create_index("ix_stock_count_lines_stock_count_id", "stock_count_lines", ["stock_count_id"])
    op.create_index("ix_stock_count_lines_ingredient_id", "stock_count_lines", ["ingredient_id"])

    with op.batch_alter_table("stock_movements") as batch:
        batch.add_column(sa.Column("entry_mode", sa.String(length=10), nullable=True))
        batch.add_column(_quantity("entry_quantity"))
        batch.add_column(sa.Column("stock_count_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_stock_movements_stock_count",
            "stock_counts",
            ["stock_count_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_stock_movements_stock_count_id", ["stock_count_id"])

    op.create_table(
        "waste_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        _restaurant("waste_approvals"),
        sa.Column(
            "ingredient_id",
            sa.Integer(),
            sa.ForeignKey(
                "ingredients.id", name="fk_waste_approvals_ingredient", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        _quantity("quantity", nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _user("created_by", "fk_waste_approvals_created_by", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _user("decided_by", "fk_waste_approvals_decided_by", nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(length=200), nullable=False),
        sa.Column(
            "movement_id",
            sa.Integer(),
            sa.ForeignKey(
                "stock_movements.id", name="fk_waste_approvals_movement", ondelete="RESTRICT"
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_waste_approvals_restaurant_status", "waste_approvals", ["restaurant_id", "status"]
    )
    op.create_index("ix_waste_approvals_ingredient_id", "waste_approvals", ["ingredient_id"])


def downgrade() -> None:
    op.drop_index("ix_waste_approvals_ingredient_id", table_name="waste_approvals")
    op.drop_index("ix_waste_approvals_restaurant_status", table_name="waste_approvals")
    op.drop_table("waste_approvals")
    with op.batch_alter_table("stock_movements") as batch:
        batch.drop_index("ix_stock_movements_stock_count_id")
        batch.drop_constraint("fk_stock_movements_stock_count", type_="foreignkey")
        batch.drop_column("stock_count_id")
        batch.drop_column("entry_quantity")
        batch.drop_column("entry_mode")
    op.drop_index("ix_stock_count_lines_ingredient_id", table_name="stock_count_lines")
    op.drop_index("ix_stock_count_lines_stock_count_id", table_name="stock_count_lines")
    op.drop_table("stock_count_lines")
    op.drop_index("uq_stock_counts_one_draft", table_name="stock_counts")
    op.drop_table("stock_counts")
    with op.batch_alter_table("ingredients") as batch:
        batch.drop_column("empty_weight_g")
        batch.drop_column("full_weight_g")
        batch.drop_column("package_label")
        batch.drop_column("package_size")
    with op.batch_alter_table("restaurants") as batch:
        batch.drop_column("waste_approval_threshold")
