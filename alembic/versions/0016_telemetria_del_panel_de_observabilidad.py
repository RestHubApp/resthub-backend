"""telemetría del panel de observabilidad.

- `obs_requests`: una fila por petición HTTP (salvo el sondeo de vida, los
  avisos en tiempo real y el propio panel), con la plantilla de la ruta, el
  estado, la duración, el tiempo en la base y quién la hizo.
- `obs_events`: los eventos de log de nivel `warning` o `error`, con sus campos
  ya sin datos sensibles y el traceback de las excepciones no controladas.

Son telemetría, no negocio: sin claves foráneas a restaurantes ni a cuentas,
para que sobrevivan a sus bajas; `restaurant_id` y `account_id` son enteros
sueltos. Se borran solas pasada la retención (`OBSERVABILITY_RETENTION_DAYS`).
Deshacerla borra las dos tablas y nada más.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _telemetry_id() -> sa.types.TypeEngine[int]:
    # 64 bits en PostgreSQL; en SQLite, `INTEGER` para que sea el `rowid`.
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "obs_requests",
        sa.Column("id", _telemetry_id(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("route", sa.String(length=255), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("db_ms", sa.Float(), nullable=False),
        sa.Column("db_queries", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("account_kind", sa.String(length=16), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_obs_requests_at", "obs_requests", ["at"], unique=False)
    op.create_index("ix_obs_requests_route_at", "obs_requests", ["route", "at"], unique=False)
    op.create_index("ix_obs_requests_request_id", "obs_requests", ["request_id"], unique=False)

    op.create_table(
        "obs_events",
        sa.Column("id", _telemetry_id(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("logger", sa.String(length=120), nullable=False),
        sa.Column("event", sa.String(length=255), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("restaurant_id", sa.Integer(), nullable=True),
        sa.Column("fields", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_obs_events_at", "obs_events", ["at"], unique=False)
    op.create_index("ix_obs_events_level_at", "obs_events", ["level", "at"], unique=False)
    op.create_index("ix_obs_events_request_id", "obs_events", ["request_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_obs_events_request_id", table_name="obs_events")
    op.drop_index("ix_obs_events_level_at", table_name="obs_events")
    op.drop_index("ix_obs_events_at", table_name="obs_events")
    op.drop_table("obs_events")
    op.drop_index("ix_obs_requests_request_id", table_name="obs_requests")
    op.drop_index("ix_obs_requests_route_at", table_name="obs_requests")
    op.drop_index("ix_obs_requests_at", table_name="obs_requests")
    op.drop_table("obs_requests")
