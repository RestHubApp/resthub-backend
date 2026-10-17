"""bitácoras de solo inserción: nadie edita ni borra un asiento (RNFS-16).

`activity_log`, `platform_activity` y `ai_decisions` rechazan `UPDATE` y
`DELETE` desde la base, con un trigger: vale para cualquier usuario, también
para el dueño de las tablas (que es el usuario con el que corre la aplicación en
Railway y al que un `REVOKE` no le alcanzaría, porque puede volver a darse el
permiso). Además se les quitan esos permisos a `PUBLIC`.

En PostgreSQL es una función `forbid_log_changes()` y un trigger por tabla; en
SQLite (desarrollo local), un trigger por tabla y operación con `RAISE(ABORT)`.
Deshacerla borra los triggers y la función.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOG_TABLES = ("activity_log", "platform_activity", "ai_decisions")
OPERATIONS = ("UPDATE", "DELETE")

_PG_FUNCTION = """
CREATE OR REPLACE FUNCTION forbid_log_changes() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'La bitácora % es de solo inserción: no se edita ni se borra.', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_PG_FUNCTION)
        for table in LOG_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION forbid_log_changes()"
            )
            op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM PUBLIC")
        return
    for table in LOG_TABLES:
        for operation in OPERATIONS:
            op.execute(
                f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'La bitácora es de solo inserción.'); END"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in LOG_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute("DROP FUNCTION IF EXISTS forbid_log_changes()")
        return
    for table in LOG_TABLES:
        for operation in OPERATIONS:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{operation.lower()}")
