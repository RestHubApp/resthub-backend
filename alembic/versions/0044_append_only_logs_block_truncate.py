"""bitácoras de solo inserción: tampoco se vacían con `TRUNCATE` (RNFS-16).

La `0034` rechaza `UPDATE` y `DELETE` fila por fila, pero en PostgreSQL un
`TRUNCATE` no dispara los triggers de fila: el dueño de las tablas (el usuario
con el que corre la aplicación en Railway) podía vaciar `activity_log`,
`platform_activity` o `ai_decisions` de un golpe. Aquí cada una recibe un trigger
de sentencia `BEFORE TRUNCATE` con la misma función `forbid_log_changes()`.

En SQLite no hay `TRUNCATE`: un `DELETE` sin `WHERE` ya pasa por los triggers de
la `0034`, así que no hace nada. Deshacerla borra los triggers nuevos.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOG_TABLES = ("activity_log", "platform_activity", "ai_decisions")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in LOG_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION forbid_log_changes()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in LOG_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
