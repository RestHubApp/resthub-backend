"""Acciones sobre los procesos: el backend y PostgreSQL."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

from resthub_chaos.config import BACKEND, PGDATA, RUN, env_sh

PID_BACKEND = RUN / "backend.pid"


def _pg_env() -> dict[str, str]:
    root = str(Path("~/.local/pgsql/root/usr").expanduser())
    env = dict(os.environ)
    env["PATH"] = f"{root}/lib/postgresql/18/bin:{env.get('PATH', '')}"
    env["LD_LIBRARY_PATH"] = f"{root}/lib/x86_64-linux-gnu:{env.get('LD_LIBRARY_PATH', '')}"
    return env


def pid_backend() -> int | None:
    try:
        pid = int(PID_BACKEND.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def remember_backend_pid() -> int:
    """Anota el PID del backend antes de la falla, para probar que no se reinició."""
    pid = pid_backend()
    if pid is None:
        message = "El backend no está corriendo."
        raise RuntimeError(message)
    (RUN / "backend.pid.antes").write_text(str(pid))
    return pid


def backend_not_restarted() -> bool:
    """Verdadero si el backend es el mismo proceso que antes de la falla (o no hubo falla)."""
    before = RUN / "backend.pid.antes"
    if not before.exists():
        return pid_backend() is not None
    return pid_backend() == int(before.read_text().strip())


def kill_backend(signal_name: str = "SIGKILL") -> int:
    pid = pid_backend()
    if pid is None:
        message = "El backend no está corriendo."
        raise RuntimeError(message)
    os.kill(pid, getattr(signal, signal_name))
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    PID_BACKEND.unlink(missing_ok=True)
    return pid


def start_backend(wait_s: float = 30.0) -> int:
    subprocess.run([str(env_sh()), "backend-start"], check=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{BACKEND}/api/v1/health", timeout=2.0).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    pid = pid_backend()
    if pid is None:
        message = "El backend no volvió a arrancar."
        raise RuntimeError(message)
    return pid


def stop_database_immediately() -> str:
    """`pg_ctl stop -m immediate`: la base cae como en un corte de luz, sin cerrar nada."""
    output = subprocess.run(
        ["pg_ctl", "-D", PGDATA, "-m", "immediate", "stop"],
        env=_pg_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    return (output.stdout + output.stderr).strip()


def start_database() -> str:
    status = subprocess.run(
        ["pg_ctl", "-D", PGDATA, "status"], env=_pg_env(), capture_output=True, check=False
    )
    if status.returncode == 0:
        return "ya estaba arriba"
    output = subprocess.run(
        [
            "pg_ctl",
            "-D",
            PGDATA,
            "-o",
            "-p 55205 -k /tmp",
            "-l",
            str(RUN / "postgres.log"),
            "-w",
            "start",
        ],
        env=_pg_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return output.stdout.strip()


def wait(seconds: float) -> float:
    time.sleep(seconds)
    return seconds
