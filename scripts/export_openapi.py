"""Vuelca el esquema OpenAPI a un archivo.

Existe para que el contrato se pueda derivar sin levantar el servidor. Un paso
de integración continua no debería tener que arrancar Uvicorn y adivinar cuándo
está listo solo para leer un JSON estático.

Uso:
    uv run python scripts/export_openapi.py openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from resthub.main import create_app

DEFAULT_DESTINATION = Path("openapi.json")


def export(destination: Path) -> Path:
    schema = create_app().openapi()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def main(argv: list[str]) -> int:
    destination = Path(argv[1]) if len(argv) > 1 else DEFAULT_DESTINATION
    written = export(destination)
    print(f"Esquema OpenAPI escrito en {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
