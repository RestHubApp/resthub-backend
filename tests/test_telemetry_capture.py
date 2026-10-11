"""Captura de la telemetría: qué se guarda de cada petición y de cada aviso, y qué nunca."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from resthub.core.config import get_settings
from resthub.core.db_timing import instrument
from resthub.core.logs import get_logger
from resthub.core.redaction import REDACTED, is_sensitive_key, redact
from resthub.core.request_logging import REQUEST_ID_HEADER, UNMATCHED_ROUTE, route_template
from resthub.core.telemetry import (
    MAX_TRACEBACK_LENGTH,
    EventRecord,
    RequestRecord,
    TelemetryRecorder,
    format_traceback,
    get_telemetry,
    set_telemetry,
)
from resthub.modules.platform.adapters.persistence.models import ObsEventRow, ObsRequestRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_telemetry_sink import (
    SqlAlchemyTelemetrySink,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import (
    API_PREFIX,
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    staffed_restaurant,
    url,
)
from tests.fakes import MemoryTelemetrySink

API = API_PREFIX
FAILING_URL = f"{API}/_prueba/falla"
UNAVAILABLE_URL = f"{API}/_prueba/no-disponible"


@pytest.fixture
def sink(session: AsyncSession) -> SqlAlchemyTelemetrySink:
    return SqlAlchemyTelemetrySink(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    )


@pytest.fixture
def telemetry(session: AsyncSession, sink: SqlAlchemyTelemetrySink) -> Iterator[TelemetryRecorder]:
    # La base de la prueba no es la del motor de la aplicación: se le suma la
    # misma medición de tiempos en la base.
    assert isinstance(session.bind, AsyncEngine)
    instrument(session.bind)
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    set_telemetry(recorder)
    yield recorder
    set_telemetry(None)


@pytest.fixture
async def platform_admin(session: AsyncSession) -> PlatformAdmin:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    return admin


def _platform_headers(admin: PlatformAdmin) -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN_SERVICE.issue_platform(admin.id or 0).value}"}


async def _requests(session: AsyncSession) -> list[ObsRequestRow]:
    return list((await session.execute(select(ObsRequestRow).order_by(ObsRequestRow.id))).scalars())


async def _events(session: AsyncSession) -> list[ObsEventRow]:
    return list((await session.execute(select(ObsEventRow).order_by(ObsEventRow.id))).scalars())


def _fields(row: ObsEventRow) -> dict[str, object]:
    return dict(json.loads(row.fields))


def _request(at: datetime, **changes: object) -> RequestRecord:
    values: dict[str, object] = {
        "at": at,
        "method": "GET",
        "route": url("orders"),
        "status": 200,
        "duration_ms": 10.0,
        "db_ms": 1.0,
        "db_queries": 1,
        "request_id": "req-00000001",
        "account_kind": "staff",
        "restaurant_id": 1,
        "account_id": 1,
    }
    return RequestRecord(**{**values, **changes})


# --- Peticiones ---------------------------------------------------------------


async def test_request_is_stored_with_its_template_timings_and_account(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    restaurant_a: StaffedRestaurant,
) -> None:
    response = await client.get(
        f"{API}/orders/987654", headers=authorization_for(restaurant_a.admin)
    )
    assert response.status_code == 404
    ok = await client.get(f"{API}/restaurant", headers=authorization_for(restaurant_a.waiter))
    assert ok.status_code == 200

    await telemetry.flush()

    failed, successful = await _requests(session)
    assert failed.route == url("orders/{order_id}")
    assert failed.method == "GET"
    assert failed.status == 404
    assert failed.request_id == response.headers[REQUEST_ID_HEADER]
    assert failed.account_kind == "staff"
    assert failed.restaurant_id == restaurant_a.id
    assert failed.account_id == restaurant_a.admin.id
    # Al menos la lectura de la identidad y la del pedido.
    assert failed.db_queries >= 2
    assert failed.db_ms >= 0
    assert failed.duration_ms >= failed.db_ms
    assert successful.route == url("restaurant")
    assert successful.status == 200
    assert successful.account_id == restaurant_a.waiter.id


async def test_platform_and_anonymous_accounts_are_told_apart(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    platform_admin: PlatformAdmin,
) -> None:
    await client.get(f"{API}/platform/auth/me", headers=_platform_headers(platform_admin))
    await client.get(f"{API}/menu")
    await client.get(f"{API}/no-existe/123")

    await telemetry.flush()

    platform_row, anonymous_row, unmatched_row = await _requests(session)
    assert (platform_row.account_kind, platform_row.account_id, platform_row.restaurant_id) == (
        "platform",
        platform_admin.id,
        None,
    )
    assert platform_row.route == url("platform/auth/me")
    assert (anonymous_row.account_kind, anonymous_row.status, anonymous_row.account_id) == (
        "anonymous",
        401,
        None,
    )
    assert unmatched_row.route == UNMATCHED_ROUTE
    assert unmatched_row.status == 404


async def test_preview_is_recorded_as_such(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    platform_admin: PlatformAdmin,
) -> None:
    sample = await staffed_restaurant(session, "muestra", is_sandbox=True)
    token = TEST_TOKEN_SERVICE.issue_preview(
        sample.waiter.id or 0, sample.id, platform_admin_id=platform_admin.id or 0
    )

    response = await client.get(
        f"{API}/restaurant", headers={"Authorization": f"Bearer {token.value}"}
    )
    assert response.status_code == 200
    await telemetry.flush()

    (row,) = await _requests(session)
    assert (row.account_kind, row.restaurant_id, row.account_id) == (
        "preview",
        sample.id,
        sample.waiter.id,
    )


async def test_probe_notices_and_panel_are_not_stored(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    platform_admin: PlatformAdmin,
) -> None:
    await client.get(f"{API}/health")
    # Sin credencial: 401, que en cualquier otra ruta dejaría un aviso.
    await client.get(f"{API}/events")
    await client.get(f"{API}/platform/observability/summary")
    await client.get(
        f"{API}/platform/observability/summary", headers=_platform_headers(platform_admin)
    )

    await telemetry.flush()

    assert await _requests(session) == []
    assert await _events(session) == []


async def test_a_4xx_leaves_no_event_and_a_5xx_does_with_the_request_id(
    app: FastAPI, client: AsyncClient, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    @app.get(UNAVAILABLE_URL)
    async def unavailable() -> JSONResponse:
        return JSONResponse({"detail": "en mantenimiento"}, status_code=503)

    rejected = await client.get(f"{API}/menu")
    down = await client.get(UNAVAILABLE_URL)
    await telemetry.flush()

    # El 401 queda solo como petición: su estado ya está en la fila.
    assert [(row.status, row.request_id) for row in await _requests(session)] == [
        (401, rejected.headers[REQUEST_ID_HEADER]),
        (503, down.headers[REQUEST_ID_HEADER]),
    ]
    (event_row,) = await _events(session)
    assert event_row.level == "error"
    assert event_row.logger == "resthub.http"
    assert event_row.event == "request.completed"
    assert event_row.request_id == down.headers[REQUEST_ID_HEADER]
    assert event_row.traceback is None


async def test_unhandled_error_is_left_as_event_with_traceback_and_its_request(
    app: FastAPI, client: AsyncClient, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    @app.get(FAILING_URL)
    async def failing() -> None:
        message = "se rompió la mesa 7"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        await client.get(FAILING_URL, headers={REQUEST_ID_HEADER: "web-falla-000001"})
    await telemetry.flush()

    (request_row,) = await _requests(session)
    assert (request_row.route, request_row.status) == (FAILING_URL, 500)
    assert request_row.request_id == "web-falla-000001"
    (event_row,) = await _events(session)
    assert (event_row.level, event_row.event) == ("error", "request.failed")
    assert event_row.request_id == "web-falla-000001"
    assert event_row.traceback is not None
    assert "RuntimeError: se rompió la mesa 7" in event_row.traceback
    assert '"error_type": "RuntimeError"' in event_row.fields
    assert "se rompió la mesa 7" in event_row.fields


async def test_from_the_error_the_panel_reaches_its_request(
    app: FastAPI,
    client: AsyncClient,
    telemetry: TelemetryRecorder,
    platform_admin: PlatformAdmin,
) -> None:
    @app.get(FAILING_URL)
    async def failing() -> None:
        message = "se rompió la mesa 7"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        await client.get(FAILING_URL, headers={REQUEST_ID_HEADER: "web-falla-000002"})
    await telemetry.flush()
    headers = _platform_headers(platform_admin)
    panel = f"{API}/platform/observability"

    logs = (await client.get(f"{panel}/logs", headers=headers, params={"level": "error"})).json()
    (entry,) = logs["items"]
    details = (await client.get(f"{panel}/logs/{entry['id']}", headers=headers)).json()
    request_rows = (
        await client.get(
            f"{panel}/requests", headers=headers, params={"request_id": entry["request_id"]}
        )
    ).json()

    assert entry["has_traceback"] is True
    assert details["fields"]["error_type"] == "RuntimeError"
    assert "RuntimeError: se rompió la mesa 7" in details["traceback"]
    assert [(item["route"], item["status"]) for item in request_rows["items"]] == [
        (FAILING_URL, 500)
    ]


async def test_query_strings_and_headers_are_never_stored(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    restaurant_a: StaffedRestaurant,
) -> None:
    headers = authorization_for(restaurant_a.admin)
    await client.get(f"{API}/orders?status=secreto-en-la-url", headers=headers)
    await client.get(f"{API}/menu?token=secreto-en-la-url")
    await telemetry.flush()

    rows = [*(await _requests(session)), *(await _events(session))]
    assert len(rows) >= 2
    stored = repr([vars(row) for row in rows])
    assert "secreto-en-la-url" not in stored
    assert headers["Authorization"].removeprefix("Bearer ") not in stored
    assert "?" not in stored


# --- Eventos de log -------------------------------------------------------------


async def test_sensitive_fields_are_hidden_at_any_depth(
    app: FastAPI, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    logger = get_logger("resthub.prueba")
    logger.warning(
        "prueba.sensible",
        password="hunter2",
        preview_code="483920",
        details={"Api_Key": "k-1", "interno": {"accessToken": "t-1"}, "mesa": 4},
        items=[{"client_secret": "s-1"}],
        restaurant_id=9,
        table=4,
    )
    logger.info("prueba.informativa", table=4)
    await telemetry.flush()

    (event_row,) = await _events(session)
    assert event_row.event == "prueba.sensible"
    assert event_row.logger == "resthub.prueba"
    assert event_row.restaurant_id == 9
    for secret in ("hunter2", "483920", "k-1", "t-1", "s-1"):
        assert secret not in event_row.fields
    details = _fields(event_row)
    assert details["password"] == REDACTED
    assert details["preview_code"] == REDACTED
    assert details["details"] == {
        "Api_Key": REDACTED,
        "interno": {"accessToken": REDACTED},
        "mesa": 4,
    }
    assert details["items"] == [{"client_secret": REDACTED}]
    assert details["table"] == 4


def test_sensitive_name_rule_compares_whole_words() -> None:
    for name in ("password", "NEW_PASSWORD", "accessToken", "X-Auth-Token", "preview_code"):
        assert is_sensitive_key(name), name
    for name in ("jwt_secret_key", "authorization", "api_key", "apiKey", "tokens"):
        assert is_sensitive_key(name), name
    for name in ("monkey", "keyboard", "status", "user_id", "encoding", 7):
        assert not is_sensitive_key(name), name
    assert redact({"a": ({"token": 1},)}) == {"a": [{"token": REDACTED}]}


def test_long_traceback_keeps_its_end() -> None:
    try:
        raise ValueError("x" * (MAX_TRACEBACK_LENGTH * 2))  # noqa: TRY301 - la prueba necesita un traceback real
    except ValueError as error:
        text = format_traceback(error)

    assert len(text) == MAX_TRACEBACK_LENGTH
    assert text.startswith("…[recortado]")
    assert text.endswith("x\n")


# --- Cola y escritura en segundo plano -------------------------------------------


async def test_with_a_full_queue_the_oldest_are_dropped_and_counted() -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(capacity=3)
    recorder.install(sink)
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)

    for number in range(5):
        recorder.record(_request(now, request_id=f"req-{number:08d}"))
    assert (recorder.pending, recorder.dropped) == (3, 2)

    await recorder.flush()
    assert [record.request_id for record in sink.requests] == [
        "req-00000002",
        "req-00000003",
        "req-00000004",
    ]


async def test_failing_batch_is_retried_once() -> None:
    sink = MemoryTelemetrySink(failures=1)
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    recorder.record(_request(datetime.now(UTC)))

    await recorder.flush()

    assert sink.attempts == 2
    assert len(sink.requests) == 1
    assert recorder.dropped == 0


async def test_batch_failing_twice_and_row_by_row_is_dropped_and_counted() -> None:
    sink = MemoryTelemetrySink(failures=10)
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    recorder.record(_request(datetime.now(UTC)))
    recorder.record(EventRecord(at=datetime.now(UTC), level="warning", logger="x", event="prueba"))

    await recorder.flush()

    # El lote dos veces y después cada fila una vez.
    assert sink.attempts == 4
    assert sink.requests == []
    assert recorder.dropped == 2
    # El aviso de la falla no vuelve a la cola: no habría dónde escribirlo.
    assert recorder.pending == 0


async def test_batches_have_at_most_batch_size_rows() -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(batch_size=2)
    recorder.install(sink)
    for _ in range(5):
        recorder.record(_request(datetime.now(UTC)))

    await recorder.flush()

    assert sink.attempts == 3
    assert len(sink.requests) == 5


async def test_background_loop_writes_on_its_own_and_drains_the_queue_on_stop() -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(flush_interval=0.01, purge_interval=3600)
    recorder.install(sink)
    await recorder.start()
    try:
        recorder.record(_request(datetime.now(UTC)))
        for _ in range(200):
            if sink.requests:
                break
            await asyncio.sleep(0.01)
        assert len(sink.requests) == 1
    finally:
        await recorder.stop()

    recorder.record(_request(datetime.now(UTC)))
    await recorder.stop()
    assert len(sink.requests) == 2


async def test_purge_deletes_what_is_past_retention(
    sink: SqlAlchemyTelemetrySink, session: AsyncSession
) -> None:
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    old, recent = now - timedelta(days=15), now - timedelta(days=13)
    await sink.write(
        [_request(old), _request(recent)],
        [
            EventRecord(at=old, level="warning", logger="x", event="viejo"),
            EventRecord(at=recent, level="warning", logger="x", event="reciente"),
        ],
    )
    recorder = TelemetryRecorder(retention_days=14)
    recorder.install(sink)

    assert await recorder.purge(now=now) == 2

    assert [row.event for row in await _events(session)] == ["reciente"]
    assert len(await _requests(session)) == 1


# --- Apagada ---------------------------------------------------------------------


async def test_disabled_captures_nothing(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(enabled=False)
    recorder.install(sink)
    set_telemetry(recorder)
    try:
        await client.get(f"{API}/restaurant", headers=authorization_for(restaurant_a.admin))
        await client.get(f"{API}/menu")
        get_logger("resthub.prueba").error("prueba.apagada")
        await recorder.flush()
    finally:
        set_telemetry(None)

    assert (sink.requests, sink.events, sink.attempts) == ([], [], 0)
    assert not recorder.active


async def test_without_a_sink_nothing_is_queued(client: AsyncClient) -> None:
    recorder = TelemetryRecorder()
    set_telemetry(recorder)
    try:
        await client.get(f"{API}/menu")
    finally:
        set_telemetry(None)

    assert recorder.pending == 0


def test_configuration_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY_RETENTION_DAYS", "3")
    get_settings.cache_clear()
    set_telemetry(None)
    try:
        assert get_settings().observability_retention_days == 3
        assert not get_telemetry().enabled
    finally:
        set_telemetry(None)
        get_settings.cache_clear()


def test_without_a_resolved_route_the_template_is_fixed() -> None:
    assert route_template({"type": "http", "path": "/x/1"}) == UNMATCHED_ROUTE


@pytest.fixture(autouse=True)
async def _no_leftover_recorder() -> AsyncIterator[None]:
    # Ninguna prueba deja instalado su registro para la siguiente.
    yield
    set_telemetry(None)
