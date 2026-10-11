"""Mide la huella de carbono del backend bajo una carga representativa, con CodeCarbon.

Levanta la aplicación real (uvicorn) sobre una SQLite nueva, migrada y sembrada, la carga con
clientes httpx que repiten los recorridos del mesero, la cocina y el encargado (los mismos de
`tests/load` y del contrato Pact) y mide con CodeCarbon, en modo sin conexión, lo que gastó la
máquina durante la carga. Informa gramos de CO2e por 1.000 peticiones y kWh, y guarda un JSON
y una fila de CSV en `reports/carbon/`.

    uv run --group carbon python scripts/measure_carbon.py
    uv run --group carbon python scripts/measure_carbon.py --scenario owner --duration 120
    uv run --group carbon python scripts/measure_carbon.py --country PER

Qué mide y qué no: CodeCarbon estima la energía de la máquina entera (CPU por TDP y carga, más
RAM) mientras corre la carga, así que incluye al servidor, a los clientes de carga y a lo que
más corra en ella. Sirve para comparar una versión con otra en la misma máquina, no como cifra
absoluta de producción. El país elige la intensidad de carbono de la red eléctrica (en modo
sin conexión se usa la tabla que trae CodeCarbon).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from codecarbon import OfflineEmissionsTracker

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "resthub123"  # noqa: S105  # nosec B105 - la contraseña pública de las cuentas de demostración
API = "/api/v1"
SCENARIOS = ("mixed", "waiter", "owner")
# Railway no fija región en `railway.toml`: el servicio corre en la región por omisión del
# proyecto, US West (Oregon). Por eso el país por omisión es Estados Unidos; con otra región
# (`Settings` del servicio en Railway), pásalo con `--country` (ISO 3166-1 alfa-3).
DEFAULT_COUNTRY = "USA"
DEFAULT_OUTPUT = ROOT / "reports" / "carbon"
# Cuatro meseros por cada encargado, como el reparto de un turno (80 % / 20 %).
MIX = ("waiter", "waiter", "waiter", "waiter", "owner")
STARTUP_TIMEOUT_SECONDS = 60
GRAMS_PER_KILOGRAM = 1000
REQUESTS_PER_REPORT = 1000


@dataclass(frozen=True, slots=True)
class Options:
    scenario: str
    duration: float
    workers: int
    think_time: float
    country: str
    region: str | None
    output_dir: Path
    port: int


@dataclass(slots=True)
class Result:
    scenario: str
    duration_seconds: float
    requests: int
    errors: int
    energy_kwh: float
    emissions_g_co2e: float
    g_co2e_per_1000_requests: float
    kwh_per_1000_requests: float
    country: str
    region: str | None
    workers: int
    started_at: str
    by_endpoint: dict[str, int]


class LoadStats:
    """Cuenta peticiones y errores desde varios hilos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.endpoints: Counter[str] = Counter()
        self.errors = 0

    def record(self, endpoint: str, *, failed: bool) -> None:
        with self._lock:
            self.endpoints[endpoint] += 1
            self.errors += int(failed)

    @property
    def requests(self) -> int:
        return sum(self.endpoints.values())


class Client:
    """Un usuario virtual: un cliente httpx con su sesión y su conteo de peticiones."""

    def __init__(self, base_url: str, stats: LoadStats) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=30.0)
        self._stats = stats
        self.token = ""

    def close(self) -> None:
        self._http.close()

    def call(
        self,
        method: str,
        path: str,
        name: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected: int = 200,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = self._http.request(method, f"{API}{path}", json=json_body, headers=headers)
        self._stats.record(f"{method} {name}", failed=response.status_code != expected)
        return response

    def login(self, email: str) -> None:
        response = self.call(
            "POST", "/auth/login", "/auth/login", json_body={"email": email, "password": PASSWORD}
        )
        response.raise_for_status()
        self.token = response.json()["access_token"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def pick_dishes(menu: dict[str, Any], count: int) -> list[dict[str, int]]:
    available = [
        item["id"]
        for category in menu.get("categories", [])
        for item in category.get("items", [])
        if item.get("is_active")
        and item.get("is_available")
        and not item.get("out_of_stock")
        and not any(group.get("min_selected", 0) > 0 for group in item.get("modifier_groups", []))
    ]
    return [
        {"menu_item_id": random.choice(available), "quantity": random.randint(1, 2)}  # noqa: S311 - carga sintética, no criptografía
        for _ in range(min(count, len(available)))
    ]


def waiter_cycle(waiter: Client, kitchen: Client, think: float) -> None:
    """Mesas, carta, pedido para llevar, cocina, servido y cobro en efectivo."""
    waiter.call("GET", "/tables", "/tables")
    menu = waiter.call("GET", "/menu", "/menu").json()
    time.sleep(think)
    opened = waiter.call(
        "POST",
        "/orders",
        "/orders",
        json_body={
            "type": "takeaway",
            "customer_name": "Cliente de carga",
            "items": pick_dishes(menu, 2),
            "client_request_id": f"carbon-{time.time_ns()}",
        },
        expected=201,
    )
    if opened.status_code != httpx.codes.CREATED:
        return
    order_id = opened.json()["id"]
    waiter.call(
        "POST",
        f"/orders/{order_id}/items",
        "/orders/{id}/items",
        json_body={"items": pick_dishes(menu, 1)},
    )
    waiter.call("POST", f"/orders/{order_id}/send", "/orders/{id}/send")
    time.sleep(think)
    kitchen.call("POST", f"/orders/{order_id}/ready", "/orders/{id}/ready")
    waiter.call("GET", f"/orders/{order_id}", "/orders/{id}")
    served = waiter.call("POST", f"/orders/{order_id}/served", "/orders/{id}/served")
    if served.status_code != httpx.codes.OK:
        return
    balance = served.json()["balance"]
    waiter.call(
        "POST",
        f"/orders/{order_id}/charge",
        "/orders/{id}/charge",
        json_body={
            "payment_method": "cash",
            "expected_balance": balance,
            "amount_received": str(math.ceil(float(balance) / 10) * 10 + 10),
        },
    )
    time.sleep(think)


def owner_cycle(owner: Client, think: float) -> None:
    """Pedidos en curso, el panel de indicadores (ocho reportes), caja e inventario."""
    today = date.today()  # noqa: DTZ011 - el rango es del día de la máquina de pruebas
    window = f"date_from={today - timedelta(days=29)}&date_to={today}"
    owner.call("GET", "/orders/active", "/orders/active")
    for report in (
        "summary",
        "sales/daily",
        "sales/hourly",
        "payments",
        "waiters",
        "dishes/top?limit=10&",
        "dishes/margins",
        "waste",
    ):
        separator = "&" if "?" in report else "?"
        owner.call(
            "GET",
            f"/insights/{report}{separator}{window}",
            f"/insights/{report.split('?')[0]}",
        )
    time.sleep(think)
    owner.call("GET", "/cash/current", "/cash/current")
    owner.call("GET", "/inventory/ingredients", "/inventory/ingredients")
    owner.call("GET", "/inventory/alerts/low-stock", "/inventory/alerts/low-stock")
    time.sleep(think)


def worker(base_url: str, role: str, think: float, stop_at: float, stats: LoadStats) -> None:
    clients = [Client(base_url, stats) for _ in range(2 if role == "waiter" else 1)]
    try:
        if role == "waiter":
            clients[0].login("mesero@resthub.dev")
            clients[1].login("cocina@resthub.dev")
        else:
            clients[0].login("admin@resthub.dev")
        while time.monotonic() < stop_at:
            if role == "waiter":
                waiter_cycle(clients[0], clients[1], think)
            else:
                owner_cycle(clients[0], think)
    finally:
        for client in clients:
            client.close()


def roles_for(scenario: str, workers: int) -> list[str]:
    if scenario != "mixed":
        return [scenario] * workers
    return [MIX[index % len(MIX)] for index in range(workers)]


def child_environment(work: Path, port: int) -> dict[str, str]:
    """El entorno de uvicorn y de la siembra: SQLite en el directorio de trabajo."""
    return {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{work / 'carbon.db'}",
        # Con `DEBUG=false` la aplicación exige PostgreSQL; esta medición corre sobre SQLite.
        "DEBUG": "true",
        "LOG_LEVEL": "WARNING",
        "LOG_JSON": "true",
        "JWT_SECRET_KEY": "medicion-de-carbono-solo-local-000000000000",
        "OPENROUTER_API_KEY": "",
        "RATE_LIMIT_PER_MINUTE": "0",
        "CORS_ALLOWED_ORIGINS": f'["http://localhost:{port}"]',
    }


def prepare_database(work: Path, port: int) -> None:
    environment = child_environment(work, port)
    for command in (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, str(ROOT / "scripts" / "seed_dev.py")],
    ):
        subprocess.run(command, cwd=ROOT, env=environment, check=True, capture_output=True)  # noqa: S603 - comandos fijos del propio repositorio


def start_server(work: Path, port: int) -> subprocess.Popen[bytes]:
    server = subprocess.Popen(  # noqa: S603 - comando fijo del propio repositorio
        [
            sys.executable,
            "-m",
            "uvicorn",
            "resthub.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=child_environment(work, port),
        # El servidor escribe un aviso por cada 409 esperado; el informe es lo que importa.
        stdout=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline and server.poll() is None:
        try:
            probe = httpx.get(f"http://127.0.0.1:{port}{API}/health", timeout=2.0)
            if probe.status_code == httpx.codes.OK:
                return server
        except httpx.HTTPError:
            time.sleep(0.5)
    server.terminate()
    message = "El servidor no respondió a tiempo."
    raise RuntimeError(message)


def open_cash_register(base_url: str) -> None:
    """Cobrar exige caja abierta: la deja abierta con el encargado de la semilla."""
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        login = http.post(
            f"{API}/auth/login", json={"email": "admin@resthub.dev", "password": PASSWORD}
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        if not http.get(f"{API}/cash/current", headers=headers).json()["is_open"]:
            http.post(
                f"{API}/cash/open",
                json={"opening_amount": "200.00", "notes": "Medición de carbono"},
                headers=headers,
            ).raise_for_status()


def run_load(options: Options, base_url: str, stats: LoadStats) -> float:
    stop_at = time.monotonic() + options.duration
    started = time.monotonic()
    threads = [
        threading.Thread(
            target=worker, args=(base_url, role, options.think_time, stop_at, stats), daemon=True
        )
        for role in roles_for(options.scenario, options.workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return time.monotonic() - started


def measure(options: Options) -> Result:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    work = Path(tempfile.mkdtemp(prefix="resthub-carbon-"))
    options.output_dir.mkdir(parents=True, exist_ok=True)
    port = options.port or free_port()
    base_url = f"http://127.0.0.1:{port}"
    server: subprocess.Popen[bytes] | None = None
    try:
        prepare_database(work, port)
        server = start_server(work, port)
        open_cash_register(base_url)
        tracker = OfflineEmissionsTracker(
            country_iso_code=options.country,
            region=options.region,
            project_name="resthub-backend",
            output_dir=str(options.output_dir),
            output_file="codecarbon-raw.csv",
            measure_power_secs=5,
            save_to_file=True,
            log_level="error",
        )
        stats = LoadStats()
        tracker.start()
        try:
            elapsed = run_load(options, base_url, stats)
        finally:
            emissions_kg = tracker.stop() or 0.0
        energy_kwh = float(tracker.final_emissions_data.energy_consumed)
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)

    per_thousand = REQUESTS_PER_REPORT / max(stats.requests, 1)
    return Result(
        scenario=options.scenario,
        duration_seconds=round(elapsed, 1),
        requests=stats.requests,
        errors=stats.errors,
        energy_kwh=energy_kwh,
        emissions_g_co2e=emissions_kg * GRAMS_PER_KILOGRAM,
        g_co2e_per_1000_requests=emissions_kg * GRAMS_PER_KILOGRAM * per_thousand,
        kwh_per_1000_requests=energy_kwh * per_thousand,
        country=options.country,
        region=options.region,
        workers=options.workers,
        started_at=started_at,
        by_endpoint=dict(stats.endpoints.most_common()),
    )


def save(result: Result, output_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = output_dir / f"carbon-{result.scenario}-{stamp}.json"
    report.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
    history = output_dir / "carbon-history.csv"
    row = {key: value for key, value in asdict(result).items() if key != "by_endpoint"}
    is_new = not history.exists()
    with history.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        if is_new:
            writer.writeheader()
        writer.writerow(row)
    return report


def parse_arguments(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--scenario", choices=SCENARIOS, default="mixed", help="Recorridos a repetir."
    )
    parser.add_argument("--duration", type=float, default=60.0, help="Segundos de carga.")
    parser.add_argument("--workers", type=int, default=5, help="Usuarios virtuales en paralelo.")
    parser.add_argument(
        "--think-time", type=float, default=0.05, help="Segundos entre pasos de un recorrido."
    )
    parser.add_argument(
        "--country",
        default=DEFAULT_COUNTRY,
        help="País de la red eléctrica, ISO 3166-1 alfa-3 (por omisión USA: la región de Railway).",
    )
    parser.add_argument(
        "--region", default=None, help="Región dentro del país, si la tabla la tiene."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--port", type=int, default=0, help="Puerto del servidor; 0 elige uno libre."
    )
    arguments = parser.parse_args(argv)
    return Options(
        scenario=arguments.scenario,
        duration=arguments.duration,
        workers=arguments.workers,
        think_time=arguments.think_time,
        country=arguments.country.upper(),
        region=arguments.region,
        output_dir=arguments.output_dir,
        port=arguments.port,
    )


def main() -> None:
    options = parse_arguments()
    result = measure(options)
    report = save(result, options.output_dir)
    print(
        f"Escenario {result.scenario}: {result.requests} peticiones en {result.duration_seconds} s"
    )
    print(f"Errores: {result.errors}")
    print(
        f"Energía: {result.energy_kwh:.6f} kWh "
        f"({result.kwh_per_1000_requests:.6f} kWh / 1000 peticiones)"
    )
    print(f"Emisiones: {result.g_co2e_per_1000_requests:.4f} gCO2e / 1000 peticiones")
    print(f"Informe: {report}")


if __name__ == "__main__":
    main()
