"""La telemetría no filtra secretos ni se deja cegar o llenar desde afuera."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from resthub.core.database import ENGINE_OPTIONS, engine
from resthub.core.logs import get_logger
from resthub.core.redaction import REDACTED, scrub_text
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
    OTHER_METHOD,
    EventRecord,
    RequestRecord,
    TelemetryRecorder,
    normalize_method,
    set_telemetry,
)
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
