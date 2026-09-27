"""Puertos y rutas del entorno de caos (ver `chaos/entorno.sh`)."""

from __future__ import annotations

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
RUN = Path(os.environ.get("RESTHUB_CHAOS_RUN", "/tmp/caos-resthub"))
EVIDENCIAS = Path(os.environ.get("RESTHUB_CHAOS_EVIDENCIAS", str(RUN / "evidencias")))

TOXIPROXY_API = os.environ.get("RESTHUB_CHAOS_TOXIPROXY", "http://127.0.0.1:8305")
# El backend directo, sin proxies: lo que ve quien está en la misma máquina.
BACKEND = os.environ.get("RESTHUB_CHAOS_BACKEND", "http://127.0.0.1:8205")
# El proxy de fallas, que es por donde entra el frontend.
FALLAS = os.environ.get("RESTHUB_CHAOS_FALLAS", "http://127.0.0.1:8307")
FRONTEND = os.environ.get("RESTHUB_CHAOS_FRONTEND", "http://localhost:5205")
# La base directa (sin Toxiproxy), para comprobar lo que quedó guardado.
BASE_DSN = os.environ.get("RESTHUB_CHAOS_DSN", "postgresql://postgres@127.0.0.1:55205/resthub")
PGDATA = os.environ.get("RESTHUB_CHAOS_PGDATA", "/tmp/pg-caos")

CLAVE = "resthub123"
ENCARGADO = "admin@resthub.dev"
MESERO = "mesero@resthub.dev"


def entorno_sh() -> Path:
    return RAIZ / "chaos" / "entorno.sh"
