"""La telemetría no filtra secretos ni se deja cegar o llenar desde afuera."""

from __future__ import annotations

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
    HTTP_METHODS,
    MAX_ACCOUNT_KIND_LENGTH,
    MAX_CONSECUTIVE_ROW_FAILURES,
    MAX_EVENT_LENGTH,
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
from resthub.modules.platform.adapters.persistence.sqlalchemy_telemetry_sink import SqlTelemetrySink
from tests.fakes import MemoryTelemetrySink

EMAIL = "juan@x.com"
PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuvSECRETOhash012345678901234567890"


@pytest.fixture
def sink(session: AsyncSession) -> SqlTelemetrySink:
    return SqlTelemetrySink(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    )


@pytest.fixture
def telemetry(sink: SqlTelemetrySink) -> Iterator[TelemetryRecorder]:
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    set_telemetry(recorder)
    yield recorder
    set_telemetry(None)


@pytest.fixture(autouse=True)
async def _sin_registro_ajeno() -> AsyncIterator[None]:
    yield
    set_telemetry(None)


async def _events(session: AsyncSession) -> list[ObsEventRow]:
    return list((await session.execute(select(ObsEventRow).order_by(ObsEventRow.id))).scalars())


def _request(**changes: object) -> RequestRecord:
    values: dict[str, object] = {
        "at": datetime.now(UTC),
        "method": "GET",
        "route": "/api/v1/orders",
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


def test_el_motor_de_la_aplicacion_no_copia_los_parametros_en_sus_errores() -> None:
    assert ENGINE_OPTIONS["hide_parameters"] is True
    assert engine.sync_engine.hide_parameters is True


async def test_un_motor_con_las_opciones_de_la_aplicacion_oculta_los_valores() -> None:
    hidden = create_async_engine("sqlite+aiosqlite:///:memory:", **ENGINE_OPTIONS)
    try:
        error = await _duplicate_admin(hidden)
    finally:
        await hidden.dispose()

    assert EMAIL not in str(error)
    assert "SQL parameters hidden" in str(error)


async def test_un_error_de_integridad_no_deja_los_parametros_en_el_evento(
    app: FastAPI, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    # El motor de las pruebas no oculta los parámetros: prueba la segunda barrera.
    error = await _duplicate_admin(session)
    assert EMAIL in str(error) and PASSWORD_HASH in str(error)

    try:
        raise error
    except IntegrityError:
        get_logger("resthub.prueba").exception("prueba.integridad")
    await telemetry.flush()

    (evento,) = await _events(session)
    assert evento.traceback is not None
    for texto in (evento.fields, evento.traceback):
        assert EMAIL not in texto
        assert "SECRETOhash" not in texto
    assert f"[parameters: {REDACTED}]" in evento.traceback
    assert "INSERT INTO platform_admins" in evento.traceback
    assert json.loads(evento.fields)["error_type"] == "IntegrityError"


def test_la_limpieza_de_textos_tapa_parametros_literales_y_filas() -> None:
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
            raise ValueError("value too long for type character varying(255)")
        self.requests.extend(requests)
        self.events.extend(events)


async def test_un_metodo_inventado_se_guarda_como_other(
    client: AsyncClient, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    await client.request("UNSUBSCRIBE", "/api/v1/menu")
    await client.request("get", "/api/v1/menu")
    await telemetry.flush()

    filas = list(
        (await session.execute(select(ObsRequestRow).order_by(ObsRequestRow.id))).scalars()
    )
    assert [fila.method for fila in filas] == ["OTHER", "GET"]
    assert normalize_method("PROPFIND") == OTHER_METHOD
    assert {normalize_method(method) for method in HTTP_METHODS} == HTTP_METHODS


def test_las_columnas_de_texto_miden_lo_que_recorta_la_captura() -> None:
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


async def test_cada_valor_se_recorta_a_su_columna_antes_de_encolar() -> None:
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

    (peticion,) = sink.requests
    assert peticion.method == OTHER_METHOD
    assert len(peticion.route) == MAX_ROUTE_LENGTH
    assert len(peticion.request_id) == MAX_REQUEST_ID_LENGTH
    assert len(peticion.account_kind) == MAX_ACCOUNT_KIND_LENGTH
    assert (peticion.restaurant_id, peticion.account_id) == (None, None)
    (evento,) = sink.events
    assert len(evento.level) <= MAX_LEVEL_LENGTH
    assert len(evento.logger) == MAX_LOGGER_LENGTH
    assert len(evento.event) == MAX_EVENT_LENGTH
    assert "\x00" not in evento.event
    evento.event.encode("utf-8")
    assert evento.request_id is not None and len(evento.request_id) == MAX_REQUEST_ID_LENGTH
    assert evento.restaurant_id is None
    assert evento.traceback == "t"


async def test_un_lote_con_una_fila_mala_pierde_solo_esa_fila() -> None:
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


async def test_con_la_base_caida_no_se_insiste_fila_por_fila_con_todo_el_lote() -> None:
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


async def test_pasado_el_tope_por_minuto_las_filas_se_descartan_y_se_cuentan() -> None:
    reloj = _Monotonic()
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(max_rows_per_minute=3, monotonic=reloj)
    recorder.install(sink)

    for numero in range(5):
        recorder.record(_request(request_id=f"req-{numero:08d}"))
    assert (recorder.pending, recorder.dropped) == (3, 2)

    # A tres por minuto, en 20 s vuelve una ficha.
    reloj.now += 20
    recorder.record(_request(request_id="req-00000005"))
    recorder.record(_request(request_id="req-00000006"))
    assert (recorder.pending, recorder.dropped) == (4, 3)

    # El balde no junta más de un minuto de fichas.
    reloj.now += 3_600
    for _ in range(10):
        recorder.record(_request())
    assert (recorder.pending, recorder.dropped) == (7, 10)


async def test_sin_tope_por_minuto_no_se_descarta_nada() -> None:
    recorder = TelemetryRecorder(max_rows_per_minute=0)
    recorder.install(MemoryTelemetrySink())
    for _ in range(50):
        recorder.record(_request())
    assert (recorder.pending, recorder.dropped) == (50, 0)


async def test_de_las_peticiones_sin_ruta_se_guarda_una_muestra() -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder()
    recorder.install(sink)

    for numero in range(25):
        recorder.record(_request(route=UNMATCHED_ROUTE, request_id=f"req-{numero:08d}"))
        recorder.record(_request(request_id=f"ruta-{numero:08d}"))
    await recorder.flush()

    sin_ruta = [record.request_id for record in sink.requests if record.route == UNMATCHED_ROUTE]
    assert sin_ruta == ["req-00000000", "req-00000010", "req-00000020"]
    assert UNMATCHED_SAMPLE_EVERY == 10
    assert len(sink.requests) == 25 + 3
    # La muestra es a propósito: no cuenta como perdido.
    assert recorder.dropped == 0


def test_el_tope_por_minuto_se_configura(monkeypatch: pytest.MonkeyPatch) -> None:
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

FAILING_URL = "/api/v1/_prueba/falla/{mesa_id}"


async def test_un_500_se_guarda_una_vez_aunque_uvicorn_lo_vuelva_a_loguear(
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
    async def falla(mesa_id: int) -> None:
        get_logger("resthub.prueba").warning("prueba.antes_de_fallar", mesa=mesa_id)
        raise RuntimeError("se rompió la mesa")

    with pytest.raises(RuntimeError) as caught:
        await client.get(
            "/api/v1/_prueba/falla/987654321", headers={REQUEST_ID_HEADER: "web-00000001"}
        )
    # Lo que hace uvicorn con la excepción que le llega de la aplicación, en el
    # mismo contexto de la petición.
    logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=caught.value)
    # Fuera de una petición, un error del servidor sí se guarda.
    contextvars.Context().run(logging.getLogger("uvicorn.error").error, "servidor.sin_peticion")
    await telemetry.flush()

    eventos = await _events(session)
    assert [(evento.logger, evento.event) for evento in eventos] == [
        ("resthub.prueba", "prueba.antes_de_fallar"),
        ("resthub.http", "request.failed"),
        ("uvicorn.error", "servidor.sin_peticion"),
    ]
    aviso, fallo, _ = eventos
    assert fallo.request_id == aviso.request_id == "web-00000001"
    for evento in (aviso, fallo):
        campos = json.loads(evento.fields)
        # El método y la ruta cruda (con el id) ya están en la fila de la petición.
        assert not {"path", "method", "request_id", "restaurant_id"} & campos.keys()
        assert "falla/987654321" not in evento.fields
    assert json.loads(aviso.fields) == {"mesa": 987654321}


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
def test_nombres_sensibles_nuevos(name: str) -> None:
    assert is_sensitive_key(name)


@pytest.mark.parametrize(
    "name", ["invoice_code", "status_code", "shipping", "ping", "session", "author", "hashtag"]
)
def test_nombres_que_no_esconden_secretos(name: str) -> None:
    assert not is_sensitive_key(name)


def test_los_valores_que_parecen_credenciales_se_tapan_en_cualquier_campo() -> None:
    campos = redact(
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

    texto = json.dumps(campos)
    for secreto in ("c2lnbmF0dXJh", "abc.def-123", "dXN1YXJp", "tok-secreto-99", "clave-db"):
        assert secreto not in texto
    assert "clave-redis" not in texto
    assert campos["detalle"] == f"falló con {REDACTED} al reintentar"
    assert campos["cabecera"] == f"Authorization: Bearer {REDACTED}"
    assert campos["nubefact"] == f'Token token="{REDACTED}"'
    assert campos["url"] == f"no conecta a postgresql+asyncpg://{REDACTED}@db.internal:5432/resthub"
    assert campos["otra"][1] == "mesa 4"
    assert campos["mesa"] == 4


async def test_el_mensaje_y_el_traceback_de_un_error_se_limpian(
    app: FastAPI, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    try:
        raise ConnectionError(f"postgresql://resthub:clave-db@db:5432 rechazó Bearer {JWT}")
    except ConnectionError:
        get_logger("resthub.prueba").exception(f"prueba.error con {JWT}", invoice_code="B001-7")
    await telemetry.flush()

    (evento,) = await _events(session)
    assert evento.traceback is not None
    for texto in (evento.fields, evento.traceback, evento.event):
        assert "clave-db" not in texto
        assert "c2lnbmF0dXJh" not in texto
    assert evento.event == f"prueba.error con {REDACTED}"
    # El número de comprobante no es un secreto y se deja ver.
    assert json.loads(evento.fields)["invoice_code"] == "B001-7"


# --- Purga ---------------------------------------------------------------------------------


async def _seed_old_and_new(sink: SqlTelemetrySink, now: datetime) -> None:
    viejo, reciente = now - timedelta(days=30), now - timedelta(days=1)
    await sink.write(
        [*(_request(at=viejo) for _ in range(5)), _request(at=reciente)],
        [
            *(EventRecord(at=viejo, level="warning", logger="x", event="viejo") for _ in range(5)),
            EventRecord(at=reciente, level="warning", logger="x", event="reciente"),
        ],
    )


async def _counts(session: AsyncSession) -> tuple[int, int]:
    requests = await session.scalar(select(func.count()).select_from(ObsRequestRow))
    events = await session.scalar(select(func.count()).select_from(ObsEventRow))
    return int(requests or 0), int(events or 0)


async def test_la_purga_borra_por_tandas(session: AsyncSession) -> None:
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    sink = SqlTelemetrySink(
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


async def test_si_otro_proceso_tiene_el_candado_la_purga_no_borra(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    sink = SqlTelemetrySink(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    )
    await _seed_old_and_new(sink, now)

    monkeypatch.setattr(sink_module, "purge_guard", lambda _: select(literal(False)))
    assert await sink.purge(now - timedelta(days=14)) == 0
    assert await _counts(session) == (6, 6)

    monkeypatch.setattr(sink_module, "purge_guard", lambda _: select(literal(True)))
    assert await sink.purge(now - timedelta(days=14)) == 10
    assert await _counts(session) == (1, 1)


def test_el_candado_de_la_purga_es_solo_de_postgresql() -> None:
    guard = sink_module.purge_guard("postgresql")
    assert guard is not None
    sql = str(guard.compile(dialect=postgresql.dialect()))
    assert "pg_try_advisory_xact_lock" in sql
    assert sink_module.purge_guard("sqlite") is None
