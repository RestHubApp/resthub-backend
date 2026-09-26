"""El panel de observabilidad por HTTP, sobre telemetría sembrada con un reloj fijo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.telemetry import EventRecord, RequestRecord, TelemetryRecorder, set_telemetry
from resthub.modules.platform.adapters.api.dependencies import get_observability_clock
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_telemetry_sink import (
    SqlTelemetrySink,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.observability import percentile, sampling_step
from resthub.modules.platform.use_cases import observability as observability_use_cases
from tests.conftest import (
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    staffed_restaurant,
)
from tests.fakes import MemoryTelemetrySink

BASE = "/api/v1/platform/observability"
# Alineado con todos los anchos de cubo (1 min a 2 h), para que las cuentas
# de la serie temporal sean exactas.
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _request(minutes_ago: float, **changes: object) -> RequestRecord:
    values: dict[str, object] = {
        "at": NOW - timedelta(minutes=minutes_ago),
        "method": "GET",
        "route": "/api/v1/orders",
        "status": 200,
        "duration_ms": 10.0,
        "db_ms": 1.0,
        "db_queries": 1,
        "request_id": "req-00000000",
        "account_kind": "staff",
        "restaurant_id": 1,
        "account_id": 1,
    }
    return RequestRecord(**{**values, **changes})


# id 1 a 6, en este orden.
REQUESTS = [
    _request(10, duration_ms=10.0, db_ms=2.0, request_id="req-00000001"),
    _request(10, duration_ms=20.0, db_ms=4.0, request_id="req-00000002"),
    _request(
        5,
        route="/api/v1/orders/{order_id}",
        status=404,
        duration_ms=30.0,
        db_ms=6.0,
        request_id="req-00000003",
    ),
    _request(
        3,
        method="POST",
        status=500,
        duration_ms=40.0,
        db_ms=8.0,
        request_id="req-00000004",
        restaurant_id=2,
        account_id=5,
    ),
    _request(
        120,
        route="/api/v1/menu",
        status=200,
        duration_ms=50.0,
        db_ms=10.0,
        request_id="req-00000005",
        account_kind="anonymous",
        restaurant_id=None,
        account_id=None,
    ),
    _request(3 * 24 * 60, duration_ms=100.0, request_id="req-00000006"),
]

# id 1 a 3, en este orden.
EVENTS = [
    EventRecord(
        at=NOW - timedelta(minutes=20),
        level="warning",
        logger="resthub.http",
        event="request.completed",
        request_id="req-00000003",
        restaurant_id=1,
        fields={"status": 404, "path": "/api/v1/orders/9"},
    ),
    EventRecord(
        at=NOW - timedelta(minutes=3),
        level="error",
        logger="resthub.http",
        event="request.failed",
        request_id="req-00000004",
        restaurant_id=2,
        fields={"error_type": "RuntimeError", "error_message": "Mesa inexistente"},
        traceback="Traceback (most recent call last):\nRuntimeError: Mesa inexistente\n",
    ),
    EventRecord(
        at=NOW - timedelta(hours=2),
        level="warning",
        logger="resthub.realtime",
        event="realtime.listen_failed",
        fields={"fallback": "local", "detalle": "100% ocupado"},
    ),
]


@pytest.fixture
def telemetry() -> Iterator[TelemetryRecorder]:
    recorder = TelemetryRecorder()
    set_telemetry(recorder)
    yield recorder
    set_telemetry(None)


@pytest.fixture
async def seeded(app: FastAPI, session: AsyncSession, telemetry: TelemetryRecorder) -> None:
    app.dependency_overrides[get_observability_clock] = lambda: lambda: NOW
    sink = SqlTelemetrySink(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    )
    await sink.write(REQUESTS, EVENTS)


@pytest.fixture
async def headers(session: AsyncSession) -> dict[str, str]:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    token = TEST_TOKEN_SERVICE.issue_platform(admin.id or 0)
    return {"Authorization": f"Bearer {token.value}"}


async def _get(client: AsyncClient, path: str, headers: dict[str, str], **params: object) -> object:
    response = await client.get(f"{BASE}{path}", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- Indicadores ------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
async def test_el_resumen_de_la_ultima_hora(client: AsyncClient, headers: dict[str, str]) -> None:
    assert await _get(client, "/summary", headers, window="1h") == {
        "window": "1h",
        "requests": 4,
        "errors_5xx": 1,
        "errors_4xx": 1,
        "error_rate": 0.25,
        # Interpolación lineal sobre 10, 20, 30 y 40 ms.
        "p50_ms": 25.0,
        "p95_ms": 38.5,
        "p99_ms": 39.7,
        "avg_db_ms": 5.0,
        "active_restaurants": 2,
        "dropped_events": 0,
        "sampled": False,
    }


@pytest.mark.usefixtures("seeded")
async def test_la_ventana_por_omision_es_de_24_horas_y_7d_llega_a_la_semana(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    summary = await _get(client, "/summary", headers)
    week = await _get(client, "/summary", headers, window="7d")
    six = await _get(client, "/summary", headers, window="6h")

    assert isinstance(summary, dict) and isinstance(week, dict) and isinstance(six, dict)
    assert (summary["window"], summary["requests"]) == ("24h", 5)
    assert week["requests"] == 6
    assert six["requests"] == 5


@pytest.mark.usefixtures("seeded")
async def test_el_filtro_por_restaurante(client: AsyncClient, headers: dict[str, str]) -> None:
    summary = await _get(client, "/summary", headers, window="1h", restaurant_id=1)
    status = await _get(client, "/status", headers, window="1h", restaurant_id=2)
    logs = await _get(client, "/logs", headers, window="1h", restaurant_id=1)

    assert isinstance(summary, dict) and isinstance(logs, dict)
    assert (summary["requests"], summary["errors_5xx"], summary["errors_4xx"]) == (3, 0, 1)
    assert summary["active_restaurants"] == 1
    assert status == [{"status": 500, "count": 1}]
    assert [item["id"] for item in logs["items"]] == [1]


async def test_sin_datos_todo_es_cero(
    client: AsyncClient, headers: dict[str, str], telemetry: TelemetryRecorder
) -> None:
    summary = await _get(client, "/summary", headers, window="1h")

    assert isinstance(summary, dict)
    assert summary["requests"] == 0
    assert summary["error_rate"] == 0.0
    assert (summary["p50_ms"], summary["p95_ms"], summary["p99_ms"]) == (0.0, 0.0, 0.0)


async def test_el_resumen_cuenta_lo_que_se_descarto(
    client: AsyncClient, headers: dict[str, str], telemetry: TelemetryRecorder
) -> None:
    recorder = TelemetryRecorder(capacity=1)
    recorder.install(MemoryTelemetrySink())
    set_telemetry(recorder)
    recorder.record(_request(1))
    recorder.record(_request(1))
    recorder.record(_request(1))

    summary = await _get(client, "/summary", headers, window="1h")

    assert isinstance(summary, dict)
    assert summary["dropped_events"] == 2


@pytest.mark.usefixtures("seeded")
async def test_con_muchas_filas_los_percentiles_salen_de_una_muestra(
    client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Con un tope de 2 filas, de las 4 de la última hora se toma una de cada dos
    # por identificador: la 2 (20 ms) y la 4 (40 ms).
    monkeypatch.setattr(
        observability_use_cases, "sampling_step", lambda rows: sampling_step(rows, limit=2)
    )

    summary = await _get(client, "/summary", headers, window="1h")
    routes = await _get(client, "/routes", headers, window="1h")

    assert isinstance(summary, dict) and isinstance(routes, list)
    assert summary["sampled"] is True
    assert summary["requests"] == 4
    assert (summary["p50_ms"], summary["p95_ms"]) == (30.0, 39.0)
    by_route = {(route["method"], route["route"]): route for route in routes}
    assert all(route["sampled"] for route in routes)
    # La ruta de un pedido tiene una sola fila (la 3), que quedó fuera de la
    # muestra: se trae entera.
    assert by_route[("GET", "/api/v1/orders/{order_id}")]["p95_ms"] == 30.0
    assert by_route[("GET", "/api/v1/orders")]["p50_ms"] == 20.0


# --- Serie temporal -----------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
async def test_la_serie_de_una_hora_tiene_cubos_de_un_minuto_y_ceros_en_los_vacios(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    series = await _get(client, "/timeseries", headers, window="1h")

    assert isinstance(series, dict)
    assert series["bucket_seconds"] == 60
    assert series["sampled"] is False
    points = series["points"]
    # De 11:00 a 12:00, los dos extremos incluidos.
    assert len(points) == 61
    assert points[0]["t"] == "2026-09-26T11:00:00Z"
    assert points[-1]["t"] == "2026-09-26T12:00:00Z"
    with_data = {point["t"]: point for point in points if point["requests"]}
    assert with_data == {
        "2026-09-26T11:50:00Z": {
            "t": "2026-09-26T11:50:00Z",
            "requests": 2,
            "errors_5xx": 0,
            "p95_ms": 19.5,
        },
        "2026-09-26T11:55:00Z": {
            "t": "2026-09-26T11:55:00Z",
            "requests": 1,
            "errors_5xx": 0,
            "p95_ms": 30.0,
        },
        "2026-09-26T11:57:00Z": {
            "t": "2026-09-26T11:57:00Z",
            "requests": 1,
            "errors_5xx": 1,
            "p95_ms": 40.0,
        },
    }
    assert all(point["p95_ms"] == 0.0 for point in points if not point["requests"])


@pytest.mark.parametrize(
    ("window", "bucket_seconds", "points", "requests"),
    [("6h", 300, 73, 5), ("24h", 900, 97, 5), ("7d", 7200, 85, 6)],
)
@pytest.mark.usefixtures("seeded")
async def test_el_ancho_de_los_cubos_depende_de_la_ventana(
    client: AsyncClient,
    headers: dict[str, str],
    window: str,
    bucket_seconds: int,
    points: int,
    requests: int,
) -> None:
    series = await _get(client, "/timeseries", headers, window=window)

    assert isinstance(series, dict)
    assert series["bucket_seconds"] == bucket_seconds
    assert len(series["points"]) == points
    assert sum(point["requests"] for point in series["points"]) == requests


# --- Rutas y estados ------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
async def test_las_rutas_por_peticiones(client: AsyncClient, headers: dict[str, str]) -> None:
    routes = await _get(client, "/routes", headers, window="1h")

    assert routes == [
        {
            "method": "GET",
            "route": "/api/v1/orders",
            "requests": 2,
            "errors_5xx": 0,
            "p50_ms": 15.0,
            "p95_ms": 19.5,
            "avg_db_ms": 3.0,
            "sampled": False,
        },
        {
            "method": "POST",
            "route": "/api/v1/orders",
            "requests": 1,
            "errors_5xx": 1,
            "p50_ms": 40.0,
            "p95_ms": 40.0,
            "avg_db_ms": 8.0,
            "sampled": False,
        },
        {
            "method": "GET",
            "route": "/api/v1/orders/{order_id}",
            "requests": 1,
            "errors_5xx": 0,
            "p50_ms": 30.0,
            "p95_ms": 30.0,
            "avg_db_ms": 6.0,
            "sampled": False,
        },
    ]


@pytest.mark.usefixtures("seeded")
async def test_las_rutas_por_p95_por_errores_y_con_limite(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    by_p95 = await _get(client, "/routes", headers, window="1h", sort="p95")
    by_errors = await _get(client, "/routes", headers, window="1h", sort="errors", limit=1)

    assert isinstance(by_p95, list) and isinstance(by_errors, list)
    assert [(route["method"], route["route"]) for route in by_p95] == [
        ("POST", "/api/v1/orders"),
        ("GET", "/api/v1/orders/{order_id}"),
        ("GET", "/api/v1/orders"),
    ]
    assert [(route["method"], route["route"]) for route in by_errors] == [
        ("POST", "/api/v1/orders")
    ]


@pytest.mark.usefixtures("seeded")
async def test_los_estados_ordenados(client: AsyncClient, headers: dict[str, str]) -> None:
    assert await _get(client, "/status", headers, window="1h") == [
        {"status": 200, "count": 2},
        {"status": 404, "count": 1},
        {"status": 500, "count": 1},
    ]
    assert await _get(client, "/status", headers, window="7d") == [
        {"status": 200, "count": 4},
        {"status": 404, "count": 1},
        {"status": 500, "count": 1},
    ]


# --- Logs -----------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
async def test_los_logs_mas_nuevos_primero(client: AsyncClient, headers: dict[str, str]) -> None:
    logs = await _get(client, "/logs", headers, window="1h")

    assert logs == {
        "items": [
            {
                "id": 2,
                "at": "2026-09-26T11:57:00Z",
                "level": "error",
                "logger": "resthub.http",
                "event": "request.failed",
                "request_id": "req-00000004",
                "restaurant_id": 2,
                "has_traceback": True,
            },
            {
                "id": 1,
                "at": "2026-09-26T11:40:00Z",
                "level": "warning",
                "logger": "resthub.http",
                "event": "request.completed",
                "request_id": "req-00000003",
                "restaurant_id": 1,
                "has_traceback": False,
            },
        ],
        "next_before_id": None,
    }


@pytest.mark.parametrize(
    ("params", "ids"),
    [
        ({"level": "error"}, [2]),
        ({"level": "warning"}, [3, 1]),
        # Sin mayúsculas, en el evento o en el JSON de los campos.
        ({"search": "MESA"}, [2]),
        ({"search": "request.completed"}, [1]),
        ({"search": "fallback"}, [3]),
        # `%` y `_` se buscan tal cual, no como comodines.
        ({"search": "100%"}, [3]),
        ({"search": "_"}, [3, 2]),
        ({"search": "%%"}, []),
        ({"request_id": "req-00000004"}, [2]),
        ({"before_id": 3}, [2, 1]),
    ],
)
@pytest.mark.usefixtures("seeded")
async def test_los_filtros_de_los_logs(
    client: AsyncClient, headers: dict[str, str], params: dict[str, object], ids: list[int]
) -> None:
    logs = await _get(client, "/logs", headers, window="24h", **params)

    assert isinstance(logs, dict)
    assert [item["id"] for item in logs["items"]] == ids


@pytest.mark.usefixtures("seeded")
async def test_los_logs_se_paginan_por_identificador(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    first = await _get(client, "/logs", headers, window="24h", limit=2)
    assert isinstance(first, dict)
    assert [item["id"] for item in first["items"]] == [3, 2]
    assert first["next_before_id"] == 2

    second = await _get(
        client, "/logs", headers, window="24h", limit=2, before_id=first["next_before_id"]
    )
    assert isinstance(second, dict)
    assert [item["id"] for item in second["items"]] == [1]
    assert second["next_before_id"] is None


@pytest.mark.usefixtures("seeded")
async def test_una_entrada_trae_sus_campos_y_su_traceback(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    entry = await _get(client, "/logs/2", headers)

    assert entry == {
        "id": 2,
        "at": "2026-09-26T11:57:00Z",
        "level": "error",
        "logger": "resthub.http",
        "event": "request.failed",
        "request_id": "req-00000004",
        "restaurant_id": 2,
        "has_traceback": True,
        "fields": {"error_type": "RuntimeError", "error_message": "Mesa inexistente"},
        "traceback": "Traceback (most recent call last):\nRuntimeError: Mesa inexistente\n",
    }
    missing = await client.get(f"{BASE}/logs/99", headers=headers)
    assert missing.status_code == 404


# --- Peticiones -----------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
async def test_las_peticiones_una_por_una(client: AsyncClient, headers: dict[str, str]) -> None:
    errors = await _get(client, "/requests", headers, window="1h", status_min=500)

    assert errors == {
        "items": [
            {
                "id": 4,
                "at": "2026-09-26T11:57:00Z",
                "method": "POST",
                "route": "/api/v1/orders",
                "status": 500,
                "duration_ms": 40.0,
                "db_ms": 8.0,
                "db_queries": 1,
                "request_id": "req-00000004",
                "account_kind": "staff",
                "restaurant_id": 2,
                "account_id": 5,
            }
        ],
        "next_before_id": None,
    }


@pytest.mark.parametrize(
    ("params", "ids"),
    [
        ({}, [5, 4, 3, 2, 1]),
        ({"route": "/api/v1/orders"}, [4, 2, 1]),
        ({"status_min": 400}, [4, 3]),
        ({"request_id": "req-00000003"}, [3]),
        ({"restaurant_id": 1}, [3, 2, 1]),
        ({"before_id": 3}, [2, 1]),
        ({"window": "7d", "route": "/api/v1/orders"}, [6, 4, 2, 1]),
    ],
)
@pytest.mark.usefixtures("seeded")
async def test_los_filtros_de_las_peticiones(
    client: AsyncClient, headers: dict[str, str], params: dict[str, object], ids: list[int]
) -> None:
    params = {"window": "24h", **params}
    page = await _get(client, "/requests", headers, **params)

    assert isinstance(page, dict)
    assert [item["id"] for item in page["items"]] == ids


@pytest.mark.usefixtures("seeded")
async def test_las_peticiones_se_paginan(client: AsyncClient, headers: dict[str, str]) -> None:
    page = await _get(client, "/requests", headers, window="7d", limit=4)

    assert isinstance(page, dict)
    assert [item["id"] for item in page["items"]] == [6, 5, 4, 3]
    assert page["next_before_id"] == 3


# --- Validación y acceso ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/summary", {"window": "2h"}),
        ("/timeseries", {"window": "30d"}),
        ("/routes", {"sort": "lentitud"}),
        ("/routes", {"limit": 0}),
        ("/status", {"restaurant_id": 0}),
        ("/logs", {"level": "info"}),
        ("/logs", {"search": "x" * 121}),
        ("/logs", {"limit": 201}),
        ("/logs", {"before_id": 0}),
        ("/requests", {"status_min": 99}),
        ("/logs/0", {}),
    ],
)
async def test_parametros_invalidos_responden_422(
    client: AsyncClient,
    headers: dict[str, str],
    telemetry: TelemetryRecorder,
    path: str,
    params: dict[str, object],
) -> None:
    response = await client.get(f"{BASE}{path}", headers=headers, params=params)

    assert response.status_code == 422


PATHS = ["/summary", "/timeseries", "/routes", "/status", "/logs", "/logs/1", "/requests"]


@pytest.mark.parametrize("path", PATHS)
async def test_solo_la_plataforma_entra_al_panel(
    client: AsyncClient,
    session: AsyncSession,
    local_a: StaffedRestaurant,
    headers: dict[str, str],
    telemetry: TelemetryRecorder,
    path: str,
) -> None:
    muestra = await staffed_restaurant(session, "muestra", is_sandbox=True)
    preview = TEST_TOKEN_SERVICE.issue_preview(
        muestra.admin.id or 0, muestra.id, platform_admin_id=1
    )

    sin_token = await client.get(f"{BASE}{path}")
    de_restaurante = await client.get(f"{BASE}{path}", headers=authorization_for(local_a.admin))
    de_vista_previa = await client.get(
        f"{BASE}{path}", headers={"Authorization": f"Bearer {preview.value}"}
    )

    assert sin_token.status_code == 401
    assert de_restaurante.status_code == 401
    assert de_vista_previa.status_code == 401


# --- Percentiles ---------------------------------------------------------------------------


def test_percentil_con_interpolacion_lineal() -> None:
    assert percentile([], 0.95) == 0.0
    assert percentile([7.0], 0.5) == 7.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0
    assert percentile([float(value) for value in range(1, 101)], 0.99) == 99.0


def test_el_paso_de_muestreo_no_pasa_del_tope() -> None:
    assert sampling_step(200_000) == 1
    assert sampling_step(200_001) == 2
    assert sampling_step(1_000_000) == 5
