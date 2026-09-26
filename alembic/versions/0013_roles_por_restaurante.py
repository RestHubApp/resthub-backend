"""roles por restaurante.

- `roles`: los roles de cada local. Todo restaurante recibe un Encargado
  (`owner`) y un Mesero (`waiter`) con los permisos que hasta ahora tenía cada
  uno fijos en el código.
- `users.role` pasa a `users.role_id`: `admin` va al Encargado de su local y
  `waiter` al Mesero.

Al deshacerla, quien tenía el rol de encargado vuelve a `admin` y cualquier
otro, incluidos los roles propios, a `waiter`: es el rol con menos poder del
código anterior.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copias fijas y no importadas del código: la migración tiene que dar lo mismo
# aunque el catálogo cambie después. Del encargado solo es informativo, porque
# sus permisos se calculan del catálogo al leer.
_OWNER_PERMISSIONS = [
    "activity.read",
    "billing.issue",
    "billing.manage",
    "cash.manage",
    "customers.manage",
    "customers.read",
    "insights.read",
    "inventory.manage",
    "inventory.read",
    "menu.manage",
    "menu.read",
    "orders.charge",
    "orders.discount_any",
    "orders.manage",
    "orders.read_all",
    "orders.take",
    "reservations.manage",
    "reservations.read",
    "restaurant.manage",
    "roles.manage",
    "staff.manage",
    "tables.manage",
    "tables.read",
]
_WAITER_PERMISSIONS = [
    "billing.issue",
    "customers.manage",
    "customers.read",
    "menu.read",
    "orders.charge",
    "orders.take",
    "reservations.manage",
    "reservations.read",
    "tables.read",
]

_roles = sa.table(
    "roles",
    sa.column("restaurant_id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("name_key", sa.String),
    sa.column("kind", sa.String),
    sa.column("permissions", sa.JSON),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("name_key", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["restaurants.id"],
            name="fk_roles_restaurant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("restaurant_id", "name_key", name="uq_roles_restaurant_name"),
    )

    connection = op.get_bind()
    restaurant_ids = connection.execute(sa.text("SELECT id FROM restaurants")).scalars().all()
    now = datetime.now(UTC)
    rows = []
    for restaurant_id in restaurant_ids:
        rows.append(
            {
                "restaurant_id": restaurant_id,
                "name": "Encargado",
                "name_key": "encargado",
                "kind": "owner",
                "permissions": _OWNER_PERMISSIONS,
                "created_at": now,
            }
        )
        rows.append(
            {
                "restaurant_id": restaurant_id,
                "name": "Mesero",
                "name_key": "mesero",
                "kind": "waiter",
                "permissions": _WAITER_PERMISSIONS,
                "created_at": now,
            }
        )
    if rows:
        op.bulk_insert(_roles, rows)

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("role_id", sa.Integer(), nullable=True))

    # Un valor desconocido en `role` cae al mesero, el rol con menos poder.
    op.execute(
        "UPDATE users SET role_id = (SELECT roles.id FROM roles "
        "WHERE roles.restaurant_id = users.restaurant_id "
        "AND roles.kind = CASE WHEN users.role = 'admin' THEN 'owner' ELSE 'waiter' END)"
    )

    with op.batch_alter_table("users") as batch:
        batch.alter_column("role_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key("fk_users_role", "roles", ["role_id"], ["id"], ondelete="RESTRICT")
        batch.create_index("ix_users_role_id", ["role_id"], unique=False)
        batch.drop_column("role")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("role", sa.String(length=32), nullable=True))

    op.execute(
        "UPDATE users SET role = CASE WHEN (SELECT roles.kind FROM roles "
        "WHERE roles.id = users.role_id) = 'owner' THEN 'admin' ELSE 'waiter' END"
    )

    with op.batch_alter_table("users") as batch:
        batch.alter_column("role", existing_type=sa.String(length=32), nullable=False)
        batch.drop_index("ix_users_role_id")
        batch.drop_constraint("fk_users_role", type_="foreignkey")
        batch.drop_column("role_id")

    op.drop_table("roles")
