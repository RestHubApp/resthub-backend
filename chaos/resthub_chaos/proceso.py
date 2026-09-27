"""Acciones sobre los procesos: el backend y PostgreSQL."""

from __future__ import annotations

import os
import signal
import subprocess
import time

import httpx

from resthub_chaos.config import BACKEND, PGDATA, RUN, entorno_sh

PID_BACKEND = RUN / "backend.pid"


def _pg_env() -> dict[str, str]:
    raiz = os.path.expanduser("~/.local/pgsql/root/usr")
    env = dict(os.environ)
    env["PATH"] = f"{raiz}/lib/postgresql/18/bin:{env.get('PATH', '')}"
    env["LD_LIBRARY_PATH"] = f"{raiz}/lib/x86_64-linux-gnu:{env.get('LD_LIBRARY_PATH', '')}"
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


def recordar_pid_backend() -> int:
    """Anota el PID del backend antes de la falla, para probar que no se reinició."""
    pid = pid_backend()
    if pid is None:
        raise RuntimeError("El backend no está corriendo.")
    (RUN / "backend.pid.antes").write_text(str(pid))
    return pid


def backend_sin_reiniciar() -> bool:
    """Verdadero si el backend es el mismo proceso que antes de la falla (o no hubo falla)."""
    antes = RUN / "backend.pid.antes"
    if not antes.exists():
        return pid_backend() is not None
    return pid_backend() == int(antes.read_text().strip())


def matar_backend(senal: str = "SIGKILL") -> int:
    pid = pid_backend()
    if pid is None:
        raise RuntimeError("El backend no está corriendo.")
    os.kill(pid, getattr(signal, senal))
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    PID_BACKEND.unlink(missing_ok=True)
    return pid


def arrancar_backend(esperar_s: float = 30.0) -> int:
    subprocess.run([str(entorno_sh()), "backend-arrancar"], check=True)
    limite = time.monotonic() + esperar_s
    while time.monotonic() < limite:
        try:
            if httpx.get(f"{BACKEND}/api/v1/health", timeout=2.0).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    pid = pid_backend()
    if pid is None:
        raise RuntimeError("El backend no volvió a arrancar.")
    return pid


def detener_base_inmediato() -> str:
    """`pg_ctl stop -m immediate`: la base cae como en un corte de luz, sin cerrar nada."""
    salida = subprocess.run(
        ["pg_ctl", "-D", PGDATA, "-m", "immediate", "stop"],
        env=_pg_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    return (salida.stdout + salida.stderr).strip()


def arrancar_base() -> str:
    estado = subprocess.run(
        ["pg_ctl", "-D", PGDATA, "status"], env=_pg_env(), capture_output=True, check=False
    )
    if estado.returncode == 0:
        return "ya estaba arriba"
    salida = subprocess.run(
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
    return salida.stdout.strip()


def esperar(segundos: float) -> float:
    time.sleep(segundos)
    return segundos
