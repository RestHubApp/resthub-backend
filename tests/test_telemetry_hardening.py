"""La telemetría no filtra secretos ni se deja cegar o llenar desde afuera."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import event, func, insert, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from resthub.core.config import Settings, get_settings
from resthub.core.database import ENGINE_OPTIONS, engine
from resthub.core.logs import get_logger
from resthub.core.redaction import REDACTED, is_sensitive_key, redact, scrub_text
from resthub.core.request_logging import REQUEST_ID_HEADER
from resthub.core.telemetry import (
    CAPPED_KEY,
    CAPPED_MARK,
    HTTP_METHODS,
    MAX_ACCOUNT_KIND_LENGTH,
    MAX_CONSECUTIVE_ROW_FAILURES,
    MAX_EVENT_LENGTH,
    MAX_FIELDS_JSON_BYTES,
    MAX_LEVEL_LENGTH,
    MAX_LOGGER_LENGTH,
    MAX_METHOD_LENGTH,
    MAX_REQUEST_ID_LENGTH,
    MAX_ROUTE_LENGTH,
    MAX_ROWS_PER_MINUTE,
    OTHER_METHOD,
    UNMATCHED_ROUTE,
    UNMATCHED_SAMPLE_EVERY,
    EventRecord,
    RequestRecord,
    TelemetryRecorder,
    cap_fields,
    get_telemetry,
    normalize_method,
    set_telemetry,
)
from resthub.modules.platform.adapters.persistence import sqlalchemy_telemetry_sink as sink_module
from resthub.modules.platform.adapters.persistence.models import (
    ObsEventRow,
    ObsRequestRow,
    PlatformAdminRow,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_telemetry_sink import (
    SqlAlchemyTelemetrySink,
)
from tests.conftest import url
from tests.fakes import MemoryTelemetrySink

EMAIL = "juan@x.com"
PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuvSECRETOhash012345678901234567890"


@pytest.fixture
def sink(session: AsyncSession) -> SqlAlchemyTelemetrySink:
    return SqlAlchemyTelemetrySink(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    )


@pytest.fixture
def telemetry(sink: SqlAlchemyTelemetrySink) -> Iterator[TelemetryRecorder]:
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    set_telemetry(recorder)
    yield recorder
    set_telemetry(None)


@pytest.fixture(autouse=True)
async def _clear_telemetry_after() -> AsyncIterator[None]:
    yield
    set_telemetry(None)


async def _events(session: AsyncSession) -> list[ObsEventRow]:
    return list((await session.execute(select(ObsEventRow).order_by(ObsEventRow.id))).scalars())


def _request(**changes: object) -> RequestRecord:
    values: dict[str, object] = {
        "at": datetime.now(UTC),
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


async def _duplicate_admin(target: AsyncEngine | AsyncSession) -> IntegrityError:
    row = {"email": EMAIL, "full_name": "Juan", "password_hash": PASSWORD_HASH, "is_active": True}
    if isinstance(target, AsyncSession):
        await target.execute(insert(PlatformAdminRow), row)
        with pytest.raises(IntegrityError) as caught:
            await target.execute(insert(PlatformAdminRow), row)
        await target.rollback()
        return caught.value
    async with target.begin() as connection:
        await connection.run_sync(PlatformAdminRow.metadata.create_all)
    async with target.connect() as connection:
        await connection.execute(insert(PlatformAdminRow), row)
        with pytest.raises(IntegrityError) as caught:
            await connection.execute(insert(PlatformAdminRow), row)
    return caught.value


# --- Parámetros de las consultas --------------------------------------------------


def test_app_engine_does_not_copy_parameters_into_its_errors() -> None:
    assert ENGINE_OPTIONS["hide_parameters"] is True
    assert engine.sync_engine.hide_parameters is True


async def test_engine_with_app_options_hides_the_values() -> None:
    hidden = create_async_engine("sqlite+aiosqlite:///:memory:", **ENGINE_OPTIONS)
    try:
        error = await _duplicate_admin(hidden)
    finally:
        await hidden.dispose()

    assert EMAIL not in str(error)
    assert "SQL parameters hidden" in str(error)


async def test_integrity_error_leaves_no_parameters_in_the_event(
    app: FastAPI, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    # El motor de las pruebas no oculta los parámetros: prueba la segunda barrera.
    error = await _duplicate_admin(session)
    assert EMAIL in str(error)
    assert PASSWORD_HASH in str(error)

    try:
        raise error
    except IntegrityError:
        get_logger("resthub.prueba").exception("prueba.integridad")
    await telemetry.flush()

    (event_record,) = await _events(session)
    assert event_record.traceback is not None
    for text in (event_record.fields, event_record.traceback):
        assert EMAIL not in text
        assert "SECRETOhash" not in text
    assert f"[parameters: {REDACTED}]" in event_record.traceback
    assert "INSERT INTO platform_admins" in event_record.traceback
    assert json.loads(event_record.fields)["error_type"] == "IntegrityError"


def test_text_scrubbing_masks_parameters_literals_and_rows() -> None:
    text = (
        "(psycopg.errors.UniqueViolation) duplicate key value\n"
        "DETAIL:  Key (email)=(juan@x.com) already exists.\n"
        "[SQL: SELECT * FROM users WHERE email = 'juan@x.com' AND id = ?]\n"
        "[parameters: ('juan@x.com', 'a]b', '$2b$12$hash')]\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )
    scrubbed = scrub_text(text)

    assert "juan@x.com" not in scrubbed
    assert "$2b$12$hash" not in scrubbed
    assert f"Key (email)=({REDACTED}) already exists" in scrubbed
    assert f"email = '{REDACTED}' AND id = ?" in scrubbed
    assert "(Background on this error" in scrubbed
    # Recortado sin el corchete final: se tapa hasta el final del texto.
    assert scrub_text("x [parameters: ('juan@x.com', 'secreto") == f"x [parameters: {REDACTED}]"
    assert scrub_text("DETAIL: Failing row contains (1, juan@x.com, null).") == (
        f"DETAIL: Failing row contains ({REDACTED})"
    )


# --- Filas que no entran en su columna ---------------------------------------------


class _PickySink(MemoryTelemetrySink):
    """Rechaza, como lo haría PostgreSQL, todo lote que traiga una fila mala."""

    async def write(self, requests: Sequence[RequestRecord], events: Sequence[EventRecord]) -> None:
        self.attempts += 1
        if any("rechazada" in record.route for record in requests):
            message = "value too long for type character varying(255)"
            raise ValueError(message)
        self.requests.extend(requests)
        self.events.extend(events)


async def test_an_invented_method_is_stored_as_other(
    client: AsyncClient, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    await client.request("UNSUBSCRIBE", url("menu"))
    await client.request("get", url("menu"))
    await telemetry.flush()

    rows = list((await session.execute(select(ObsRequestRow).order_by(ObsRequestRow.id))).scalars())
    assert [row.method for row in rows] == ["OTHER", "GET"]
    assert normalize_method("PROPFIND") == OTHER_METHOD
    assert {normalize_method(method) for method in HTTP_METHODS} == HTTP_METHODS


def test_text_columns_match_what_capture_trims() -> None:
    requests, events = ObsRequestRow.__table__.c, ObsEventRow.__table__.c
    assert [
        requests.method.type.length,
        requests.route.type.length,
        requests.request_id.type.length,
        requests.account_kind.type.length,
        events.level.type.length,
        events.logger.type.length,
        events.event.type.length,
        events.request_id.type.length,
    ] == [
        MAX_METHOD_LENGTH,
        MAX_ROUTE_LENGTH,
        MAX_REQUEST_ID_LENGTH,
        MAX_ACCOUNT_KIND_LENGTH,
        MAX_LEVEL_LENGTH,
        MAX_LOGGER_LENGTH,
        MAX_EVENT_LENGTH,
        MAX_REQUEST_ID_LENGTH,
    ]
    assert len(OTHER_METHOD) <= MAX_METHOD_LENGTH


async def test_every_value_is_trimmed_to_its_column_before_queueing() -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder()
    recorder.install(sink)
    recorder.record(
        _request(
            method="M" * 50,
            route="/r" * 500,
            request_id="x" * 500,
            account_kind="k" * 50,
            restaurant_id=2**40,
            account_id=-(2**40),
        )
    )
    recorder.record(
        EventRecord(
            at=datetime.now(UTC),
            level="warning" * 5,
            logger="l" * 500,
            event="e\x00" * 500 + "\ud800",
            request_id="r" * 500,
            restaurant_id=2**33,
            traceback="t\x00",
        )
    )
    await recorder.flush()

    (request_record,) = sink.requests
    assert request_record.method == OTHER_METHOD
    assert len(request_record.route) == MAX_ROUTE_LENGTH
    assert len(request_record.request_id) == MAX_REQUEST_ID_LENGTH
    assert len(request_record.account_kind) == MAX_ACCOUNT_KIND_LENGTH
    assert (request_record.restaurant_id, request_record.account_id) == (None, None)
    (event_record,) = sink.events
    assert len(event_record.level) <= MAX_LEVEL_LENGTH
    assert len(event_record.logger) == MAX_LOGGER_LENGTH
    assert len(event_record.event) == MAX_EVENT_LENGTH
    assert "\x00" not in event_record.event
    event_record.event.encode("utf-8")
    assert event_record.request_id is not None
    assert len(event_record.request_id) == MAX_REQUEST_ID_LENGTH
    assert event_record.restaurant_id is None
    assert event_record.traceback == "t"


async def test_a_batch_with_a_bad_row_loses_only_that_row() -> None:
    sink = _PickySink()
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    recorder.record(_request(request_id="req-00000001"))
    recorder.record(_request(route="/rechazada", request_id="req-00000002"))
    recorder.record(_request(request_id="req-00000003"))

    await recorder.flush()

    # Dos veces el lote y después una vez cada fila.
    assert sink.attempts == 5
    assert [record.request_id for record in sink.requests] == ["req-00000001", "req-00000003"]
    assert recorder.dropped == 1


async def test_with_the_database_down_rows_are_not_retried_one_by_one_for_the_whole_batch() -> None:
    sink = MemoryTelemetrySink(failures=1_000)
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    for _ in range(20):
        recorder.record(_request())

    await recorder.flush()

    assert sink.attempts == 2 + MAX_CONSECUTIVE_ROW_FAILURES
    assert recorder.dropped == 20


# --- Avalanchas ----------------------------------------------------------------------


class _Monotonic:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def test_over_the_per_minute_cap_rows_are_dropped_and_counted() -> None:
    clock = _Monotonic()
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(max_rows_per_minute=3, monotonic=clock)
    recorder.install(sink)

    for number in range(5):
        recorder.record(_request(request_id=f"req-{number:08d}"))
    assert (recorder.pending, recorder.dropped) == (3, 2)

    # A tres por minuto, en 20 s vuelve una ficha.
    clock.now += 20
    recorder.record(_request(request_id="req-00000005"))
    recorder.record(_request(request_id="req-00000006"))
    assert (recorder.pending, recorder.dropped) == (4, 3)

    # El balde no junta más de un minuto de fichas.
    clock.now += 3_600
    for _ in range(10):
        recorder.record(_request())
    assert (recorder.pending, recorder.dropped) == (7, 10)


async def test_without_per_minute_cap_nothing_is_dropped() -> None:
    recorder = TelemetryRecorder(max_rows_per_minute=0)
    recorder.install(MemoryTelemetrySink())
    for _ in range(50):
        recorder.record(_request())
    assert (recorder.pending, recorder.dropped) == (50, 0)


async def test_a_sample_of_unmatched_requests_is_stored() -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder()
    recorder.install(sink)

    for number in range(25):
        recorder.record(_request(route=UNMATCHED_ROUTE, request_id=f"req-{number:08d}"))
        recorder.record(_request(request_id=f"ruta-{number:08d}"))
    await recorder.flush()

    unmatched = [record.request_id for record in sink.requests if record.route == UNMATCHED_ROUTE]
    assert unmatched == ["req-00000000", "req-00000010", "req-00000020"]
    assert UNMATCHED_SAMPLE_EVERY == 10
    assert len(sink.requests) == 25 + 3
    # La muestra es a propósito: no cuenta como perdido.
    assert recorder.dropped == 0


def test_per_minute_cap_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_MAX_ROWS_PER_MINUTE", "2")
    get_settings.cache_clear()
    set_telemetry(None)
    try:
        assert get_settings().observability_max_rows_per_minute == 2
        recorder = get_telemetry()
        recorder.install(MemoryTelemetrySink())
        for _ in range(4):
            recorder.record(_request())
        assert recorder.dropped == 2
    finally:
        set_telemetry(None)
        get_settings.cache_clear()
    default = Settings.model_fields["observability_max_rows_per_minute"].default
    assert default == MAX_ROWS_PER_MINUTE


# --- Duplicados y campos que ya tienen su columna ----------------------------------------

FAILING_URL = url("_prueba/falla/{table_id}")


async def test_a_500_is_stored_once_even_if_uvicorn_logs_it_again(
    app: FastAPI,
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Las pruebas de migraciones corren `fileConfig` de Alembic, que apaga los
    # loggers que ya existían; en producción corre en otro proceso.
    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "disabled", False)

    @app.get(FAILING_URL)
    async def fails(table_id: int) -> None:
        get_logger("resthub.prueba").warning("prueba.antes_de_fallar", table=table_id)
        message = "se rompió la mesa"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError) as caught:
        await client.get(
            url("_prueba/falla/987654321"), headers={REQUEST_ID_HEADER: "web-00000001"}
        )
    # Lo que hace uvicorn con la excepción que le llega de la aplicación, en el
    # mismo contexto de la petición.
    logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=caught.value)
    # Fuera de una petición, un error del servidor sí se guarda.
    contextvars.Context().run(logging.getLogger("uvicorn.error").error, "servidor.sin_peticion")
    await telemetry.flush()

    events = await _events(session)
    assert [(event_record.logger, event_record.event) for event_record in events] == [
        ("resthub.prueba", "prueba.antes_de_fallar"),
        ("resthub.http", "request.failed"),
        ("uvicorn.error", "servidor.sin_peticion"),
    ]
    warning_event, failure_event, _ = events
    assert failure_event.request_id == warning_event.request_id == "web-00000001"
    for event_record in (warning_event, failure_event):
        fields = json.loads(event_record.fields)
        # El método y la ruta cruda (con el id) ya están en la fila de la petición.
        assert not {"path", "method", "request_id", "restaurant_id"} & fields.keys()
        assert "falla/987654321" not in event_record.fields
    assert json.loads(warning_event.fields) == {"table": 987654321}


# --- Nombres y valores sensibles --------------------------------------------------------

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiJ9.c2lnbmF0dXJhLXNlY3JldGE"


@pytest.mark.parametrize(
    "name",
    [
        "jwt",
        "code_hash",
        "hash",
        "credential",
        "credentials",
        "auth",
        "bearer",
        "dsn",
        "database_url",
        "DATABASE_URL",
        "databaseUrl",
        "cookie",
        "cookies",
        "Set-Cookie",
        "session_id",
        "sessionId",
        "otp",
        "pin",
        "signature",
        "passcode",
        "ACCESSTOKEN",
        "PASSWORDHASH",
        "provider_token",
        "APIKEY",
    ],
)
def test_new_sensitive_names(name: str) -> None:
    assert is_sensitive_key(name)


@pytest.mark.parametrize(
    "name", ["invoice_code", "status_code", "shipping", "ping", "session", "author", "hashtag"]
)
def test_names_that_hide_no_secrets(name: str) -> None:
    assert not is_sensitive_key(name)


def test_credential_like_values_are_masked_in_any_field() -> None:
    fields = redact(
        {
            "detalle": f"falló con {JWT} al reintentar",
            "cabecera": "Authorization: Bearer abc.def-123",
            "basica": "basic dXN1YXJpbzpjbGF2ZQ==",
            "nubefact": 'Token token="tok-secreto-99"',
            "url": "no conecta a postgresql+asyncpg://resthub:clave-db@db.internal:5432/resthub",
            "otra": ["redis://:clave-redis@cache:6379/0", "mesa 4"],
            "mesa": 4,
        }
    )

    text = json.dumps(fields)
    for secret in ("c2lnbmF0dXJh", "abc.def-123", "dXN1YXJp", "tok-secreto-99", "clave-db"):
        assert secret not in text
    assert "clave-redis" not in text
    assert fields["detalle"] == f"falló con {REDACTED} al reintentar"
    assert fields["cabecera"] == f"Authorization: Bearer {REDACTED}"
    assert fields["nubefact"] == f'Token token="{REDACTED}"'
    assert fields["url"] == f"no conecta a postgresql+asyncpg://{REDACTED}@db.internal:5432/resthub"
    assert fields["otra"][1] == "mesa 4"
    assert fields["mesa"] == 4


async def test_error_message_and_traceback_are_scrubbed(
    app: FastAPI, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    try:
        message = f"postgresql://resthub:clave-db@db:5432 rechazó Bearer {JWT}"
        raise ConnectionError(message)  # noqa: TRY301 - la prueba necesita un traceback real
    except ConnectionError:
        get_logger("resthub.prueba").exception(f"prueba.error con {JWT}", invoice_code="B001-7")
    await telemetry.flush()

    (event_record,) = await _events(session)
    assert event_record.traceback is not None
    for text in (event_record.fields, event_record.traceback, event_record.event):
        assert "clave-db" not in text
        assert "c2lnbmF0dXJh" not in text
    assert event_record.event == f"prueba.error con {REDACTED}"
    # El número de comprobante no es un secreto y se deja ver.
    assert json.loads(event_record.fields)["invoice_code"] == "B001-7"


# --- Purga ---------------------------------------------------------------------------------


async def _seed_old_and_new(sink: SqlAlchemyTelemetrySink, now: datetime) -> None:
    old, recent = now - timedelta(days=30), now - timedelta(days=1)
    await sink.write(
        [*(_request(at=old) for _ in range(5)), _request(at=recent)],
        [
            *(EventRecord(at=old, level="warning", logger="x", event="viejo") for _ in range(5)),
            EventRecord(at=recent, level="warning", logger="x", event="reciente"),
        ],
    )


async def _counts(session: AsyncSession) -> tuple[int, int]:
    requests = await session.scalar(select(func.count()).select_from(ObsRequestRow))
    events = await session.scalar(select(func.count()).select_from(ObsEventRow))
    return int(requests or 0), int(events or 0)


async def test_purge_deletes_in_batches(session: AsyncSession) -> None:
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    sink = SqlAlchemyTelemetrySink(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession),
        purge_batch_size=2,
    )
    await _seed_old_and_new(sink, now)
    statements: list[str] = []

    def count(*args: object) -> None:
        if str(args[2]).startswith("DELETE"):
            statements.append(str(args[2]))

    assert isinstance(session.bind, AsyncEngine)
    event.listen(session.bind.sync_engine, "before_cursor_execute", count)
    try:
        assert await sink.purge(now - timedelta(days=14)) == 10
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", count)

    # Cinco filas de a dos por tabla: tres tandas en cada una.
    assert len(statements) == 6
    assert all("LIMIT" in statement for statement in statements)
    assert await _counts(session) == (1, 1)


async def test_if_another_process_holds_the_lock_purge_deletes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    sink = SqlAlchemyTelemetrySink(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    )
    await _seed_old_and_new(sink, now)

    monkeypatch.setattr(sink_module, "purge_guard", lambda _: select(literal(False)))
    assert await sink.purge(now - timedelta(days=14)) == 0
    assert await _counts(session) == (6, 6)

    monkeypatch.setattr(sink_module, "purge_guard", lambda _: select(literal(True)))
    assert await sink.purge(now - timedelta(days=14)) == 10
    assert await _counts(session) == (1, 1)


def test_purge_lock_is_postgresql_only() -> None:
    guard = sink_module.purge_guard("postgresql")
    assert guard is not None
    sql = str(guard.compile(dialect=postgresql.dialect()))
    assert "pg_try_advisory_xact_lock" in sql
    assert sink_module.purge_guard("sqlite") is None


# --- Tope de los campos -------------------------------------------------------------------


def test_small_fields_are_left_untouched() -> None:
    fields = {"mesa": 4, "items": list(range(10))}
    assert cap_fields(fields) is fields


async def test_huge_fields_are_capped_and_say_so(
    app: FastAPI, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    try:
        message = "se rompió la mesa 7"
        raise ValueError(message)  # noqa: TRY301 - la prueba necesita un traceback real
    except ValueError:
        get_logger("resthub.prueba").exception(
            "prueba.enorme",
            ids=list(range(20_000)),
            detail={f"clave_{number}": "x" * 1_500 for number in range(200)},
            **{f"campo_{number}": number for number in range(100)},
        )
    await telemetry.flush()

    (event_record,) = await _events(session)
    assert len(event_record.fields.encode("utf-8")) <= MAX_FIELDS_JSON_BYTES
    fields = json.loads(event_record.fields)
    assert fields[CAPPED_KEY] == CAPPED_MARK
    assert fields["error_type"] == "ValueError"
    assert fields["error_message"] == "se rompió la mesa 7"
    assert fields["ids"][-1] == {CAPPED_KEY: CAPPED_MARK}
    assert fields["detail"][CAPPED_KEY] == CAPPED_MARK


def test_if_it_still_does_not_fit_only_type_and_message_remain() -> None:
    # Cinco claves por nivel y cinco niveles: aun con el recorte más corto son
    # miles de textos.
    tree: object = "x" * 500
    for _ in range(5):
        tree = {f"rama_{number}": tree for number in range(5)}
    fields = {"error_type": "ValueError", "error_message": "m" * 5_000, "arbol": tree}

    capped = cap_fields(fields)

    assert capped == {
        "error_type": "ValueError",
        "error_message": "m" * 199 + "…",
        CAPPED_KEY: CAPPED_MARK,
    }


# --- Al apagar -------------------------------------------------------------------------------


class _SlowSink(MemoryTelemetrySink):
    """Tarda `delay` segundos en cada escritura; con `None`, no termina nunca."""

    def __init__(self, delay: float | None) -> None:
        super().__init__()
        self.delay = delay

    async def write(self, requests: Sequence[RequestRecord], events: Sequence[EventRecord]) -> None:
        self.attempts += 1
        if self.delay is None:
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(self.delay)
        self.requests.extend(requests)
        self.events.extend(events)


async def _started_writing(sink: MemoryTelemetrySink) -> None:
    for _ in range(500):
        if sink.attempts:
            return
        await asyncio.sleep(0.005)
    message = "el bucle no empezó a escribir"
    raise AssertionError(message)


async def test_on_shutdown_the_batch_being_written_is_awaited() -> None:
    sink = _SlowSink(delay=0.1)
    recorder = TelemetryRecorder(flush_interval=0.01, shutdown_timeout=2)
    recorder.install(sink)
    await recorder.start()
    recorder.record(_request())
    await _started_writing(sink)

    await recorder.stop()

    assert len(sink.requests) == 1
    assert recorder.dropped == 0


async def test_batch_cut_on_shutdown_counts_as_lost() -> None:
    sink = _SlowSink(delay=None)
    recorder = TelemetryRecorder(flush_interval=0.01, shutdown_timeout=0.05)
    recorder.install(sink)
    await recorder.start()
    recorder.record(_request())
    recorder.record(_request())
    await _started_writing(sink)

    await asyncio.wait_for(recorder.stop(), timeout=2)

    assert sink.requests == []
    assert recorder.dropped == 2


async def test_what_is_left_in_the_queue_after_the_deadline_counts_as_lost() -> None:
    sink = _SlowSink(delay=None)
    recorder = TelemetryRecorder(batch_size=1, shutdown_timeout=0.05)
    recorder.install(sink)
    for _ in range(3):
        recorder.record(_request())

    await asyncio.wait_for(recorder.stop(), timeout=2)

    # El primer lote se cortó a mitad y los otros dos no llegaron a salir.
    assert sink.attempts == 1
    assert (recorder.dropped, recorder.pending) == (3, 0)
