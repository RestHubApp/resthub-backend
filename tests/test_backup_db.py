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


def test_backup_postgres_passes_the_password_by_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record_pg_dump(monkeypatch)

    backup_db.backup_postgres(BACKUP_URL, tmp_path / "resthub-x.dump")

    (call,) = calls
    # La clave no va en los argumentos (se vería en `ps` y en el log si falla).
    assert not any("s3cr3t" in arg for arg in call["args"])
    assert call["args"][-1] == "postgresql://resthub_backup@db:5432/postgres"
    assert call["env"]["PGPASSWORD"] == "s3cr3t"


def test_backup_postgres_failure_does_not_print_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def failing_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(backup_db.shutil, "which", lambda _name: "/usr/bin/pg_dump")
    monkeypatch.setattr(backup_db.subprocess, "run", failing_run)

    with pytest.raises(SystemExit) as excinfo:
        backup_db.backup_postgres(BACKUP_URL, tmp_path / "resthub-x.dump")

    assert calls
    assert "s3cr3t" not in str(excinfo.value)
    assert "resthub_backup" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__
