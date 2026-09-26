"""La telemetría no filtra secretos ni se deja cegar o llenar desde afuera."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
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
from resthub.core.telemetry import RequestRecord, TelemetryRecorder, set_telemetry
from resthub.modules.platform.adapters.persistence.models import ObsEventRow, PlatformAdminRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_telemetry_sink import SqlTelemetrySink

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
