"""Verificación del contrato con el frontend (Pact, lado del proveedor).

El frontend genera `pacts/resthub-frontend-resthub-backend.json` con sus
pruebas de consumidor: qué peticiones hace y qué necesita de cada respuesta. El
archivo se copia a `tests/contract/pacts/` y se versiona. Esta prueba levanta la
aplicación real (con la ruta de estados de `provider_app.py`) en el puerto 8203,
sobre una SQLite propia migrada y sembrada, y reproduce cada interacción: si el
backend dejó de devolver un campo que el frontend usa, o cambió su tipo, falla.

Necesita el grupo `contract` (pact-python); sin él se omite:

    uv sync --group dev --group contract
    uv run pytest tests/contract -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pact = pytest.importorskip("pact", reason="Falta el grupo `contract`: uv sync --group contract")

ROOT = Path(__file__).resolve().parents[2]
PACT_FILE = Path(__file__).parent / "pacts" / "resthub-frontend-resthub-backend.json"
PORT = int(os.environ.get("CONTRACT_PROVIDER_PORT", "8203"))
BASE_URL = f"http://127.0.0.1:{PORT}"
STARTUP_SECONDS = 30


def _env(database: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "DEBUG": "true",
        "LOG_JSON": "false",
        "LOG_LEVEL": "WARNING",
        "JWT_SECRET_KEY": "contrato-solo-pruebas-locales-0000000000000",
        "OPENROUTER_API_KEY": "",
        "OBSERVABILITY_ENABLED": "false",
        "CORS_ALLOWED_ORIGINS": '["http://localhost:5203"]',
    }


def _run(env: dict[str, str], *args: str) -> None:
    # El ejecutable es el intérprete actual y los argumentos son constantes de la prueba.
    subprocess.run([sys.executable, *args], cwd=ROOT, env=env, check=True, capture_output=True)  # noqa: S603


def _wait_until_up(server: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if server.poll() is not None:
            msg = f"uvicorn terminó al arrancar (código {server.returncode})"
            raise RuntimeError(msg)
        try:
            if httpx.get(f"{BASE_URL}/api/v1/health", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.2)
    msg = f"el proveedor no respondió en {STARTUP_SECONDS} s"
    raise RuntimeError(msg)


@pytest.fixture(scope="module")
def provider(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """La aplicación con la ruta de estados, sobre una base nueva, en el puerto del contrato."""
    env = _env(tmp_path_factory.mktemp("contrato") / "provider.db")
    _run(env, "-m", "alembic", "upgrade", "head")
    _run(env, "scripts/seed_dev.py")
    server = subprocess.Popen(  # noqa: S603 - uvicorn con el intérprete actual y argumentos fijos
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.contract.provider_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        cwd=ROOT,
        env=env,
    )
    try:
        _wait_until_up(server)
        yield BASE_URL
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_el_backend_cumple_el_contrato_del_frontend(provider: str) -> None:
    assert PACT_FILE.is_file(), f"Falta el contrato {PACT_FILE}"
    verifier = (
        pact.Verifier("resthub-backend", host="127.0.0.1")
        .add_transport(url=provider)
        .add_source(PACT_FILE)
        .state_handler(f"{provider}/_pact/provider-states", body=True)
        .set_coloured_output(enabled=False)
    )
    try:
        verifier.verify()
    finally:
        print(verifier.output(strip_ansi=True))
