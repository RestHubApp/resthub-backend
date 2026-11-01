"""El workflow del respaldo usa un pg_dump que alcanza la versión del servidor."""

from __future__ import annotations

import re

from tests.test_retention_periods import BACKUP_WORKFLOW, ROOT, _step


def test_backup_workflow_pg_dump_matches_production_major() -> None:
    workflow = BACKUP_WORKFLOW.read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    production = re.search(r"image: postgres:(\d+)", ci)
    assert production is not None
    major = production.group(1)
    install = _step(workflow, "Instalar el cliente de PostgreSQL y age")
    backup = _step(workflow, "Respaldar")

    # Ubuntu solo trae el cliente 16: se pide el de PGDG con la versión de producción.
    assert f"PG_MAJOR: '{major}'" in workflow
    assert "apt.postgresql.org.sh" in install
    assert '"postgresql-client-$PG_MAJOR"' in install
    # pg_wrapper elegiría el 16 por el clúster de la imagen: el binario pedido va primero.
    assert 'echo "/usr/lib/postgresql/$PG_MAJOR/bin" >> "$GITHUB_PATH"' in install
    # Y antes de volcar se comprueba que el pg_dump del PATH sea esa versión.
    assert "pg_dump --version" in backup
    assert backup.index("pg_dump --version") < backup.index("scripts/backup_db.py")
