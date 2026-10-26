"""bitácoras: un asiento de más de dos años sí se puede borrar (RNFS-16, Ley N.º 29733).

La `0034` rechaza todo `UPDATE` y todo `DELETE` en `activity_log`,
`platform_activity` y `ai_decisions`, así que ninguna purga podía cumplir el plazo
de conservación de dos años (`docs/clasificacion-de-datos.md`). Aquí el `DELETE`
pasa solo para los asientos con más de `MIN_AGE_DAYS` días, y la base es quien
mide la edad: aunque alguien tome la aplicación, no puede borrar lo reciente.
`UPDATE` y `TRUNCATE` (`0044`) siguen prohibidos.

En PostgreSQL, el trigger de la `0034` se parte en dos: uno de `UPDATE` sin
condición y uno de `DELETE` con `WHEN`. En SQLite se rehace el de `DELETE` con
`WHEN`. Deshacerla devuelve los triggers de la `0034`.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tabla → columna con el momento del asiento.
LOG_TABLES = {
    "activity_log": "occurred_at",
    "platform_activity": "created_at",
    "ai_decisions": "created_at",
}
# Dos años. `AUDIT_LOG_RETENTION_DAYS` no puede bajar de aquí (`core/config.py`).
MIN_AGE_DAYS = 730


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table, moment in LOG_TABLES.items():
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
            op.execute(
                f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION forbid_log_changes()"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_recent_delete BEFORE DELETE ON {table} "
                f"FOR EACH ROW WHEN (OLD.{moment} > now() - interval '{MIN_AGE_DAYS} days') "
                "EXECUTE FUNCTION forbid_log_changes()"
            )
        return
    for table, moment in LOG_TABLES.items():
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"WHEN OLD.{moment} > datetime('now', '-{MIN_AGE_DAYS} days') "
            "BEGIN SELECT RAISE(ABORT, 'La bitácora es de solo inserción.'); END"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in LOG_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table}")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_recent_delete ON {table}")
            op.execute(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION forbid_log_changes()"
            )
        return
    for table in LOG_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'La bitácora es de solo inserción.'); END"
        )
