"""El respaldo de la base (`scripts/backup_db.py`) sin levantar un PostgreSQL."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import backup_db

BACKUP_URL = "postgresql+asyncpg://resthub_backup:s3cr3t@db:5432/postgres"


def _record_pg_dump(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Reemplaza `pg_dump` por uno que anota con qué se lo llamó."""
    calls: list[dict[str, Any]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(backup_db.shutil, "which", lambda _name: "/usr/bin/pg_dump")
    monkeypatch.setattr(backup_db.subprocess, "run", fake_run)
    return calls


def test_backup_postgres_dumps_only_the_public_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record_pg_dump(monkeypatch)

    backup_db.backup_postgres(BACKUP_URL, tmp_path / "resthub-x.dump")

    (call,) = calls
    # Sin el filtro, pg_dump también bloquea auth.* y storage.* en Supabase y falla
    # con `resthub_backup`, que solo puede leer `public`.
    assert "--schema=public" in call["args"]
    assert call["args"][-1].startswith("postgresql://")
