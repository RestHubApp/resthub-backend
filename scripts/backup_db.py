"""Respaldo de la base de datos.

Con PostgreSQL llama a `pg_dump` en formato personalizado (se restaura con
`pg_restore`); con SQLite copia la base con la API de respaldo de sqlite3, que
es segura aunque la aplicación esté escribiendo. Guarda el archivo con fecha y
hora en la carpeta indicada y borra los más viejos, dejando los últimos N.

    uv run python scripts/backup_db.py --dir backups --keep 14

La URL sale de `DATABASE_URL`, igual que la aplicación. Lo corre a diario la
tarea programada `.github/workflows/backup.yml`; también sirve a mano antes de
una migración delicada.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from resthub.core.config import get_settings

PREFIX = "resthub-"


def _postgres_dsn(url: str) -> str:
    # `pg_dump` no entiende el controlador asíncrono de la URL de SQLAlchemy.
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def backup_postgres(url: str, target: Path) -> None:
    if shutil.which("pg_dump") is None:
        raise SystemExit("No se encontró pg_dump: instala el cliente de PostgreSQL.")
    # Comando fijo y sin shell: la URL no se interpreta.
    subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(target),
            _postgres_dsn(url),
        ],
        check=True,
    )


def backup_sqlite(url: str, target: Path) -> None:
    source = make_url(url).database or ""
    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(target)) as copy:
        origin.backup(copy)


def rotate(folder: Path, keep: int) -> list[Path]:
    """Borra los respaldos más viejos y deja los últimos `keep`."""
    backups = sorted(folder.glob(f"{PREFIX}*"), key=lambda path: path.name)
    removed = backups[:-keep] if keep > 0 else []
    for path in removed:
        path.unlink()
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Respalda la base de datos de RestHub.")
    parser.add_argument("--dir", default="backups", help="Carpeta de destino")
    parser.add_argument("--keep", type=int, default=14, help="Cuántos respaldos conservar")
    args = parser.parse_args()

    url = get_settings().database_url
    folder = Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    postgres = url.startswith("postgresql")
    target = folder / f"{PREFIX}{stamp}.{'dump' if postgres else 'sqlite3'}"

    if postgres:
        backup_postgres(url, target)
    else:
        backup_sqlite(url, target)
    removed = rotate(folder, args.keep)
    print(f"Respaldo escrito en {target} ({target.stat().st_size} bytes).")
    if removed:
        print(f"Se borraron {len(removed)} respaldos viejos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
