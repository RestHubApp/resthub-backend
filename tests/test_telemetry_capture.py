"""Captura de la telemetría: qué se guarda de cada petición y de cada aviso, y qué nunca."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
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
from resthub.modules.platform.adapters.persistence.sqlalchemy_telemetry_sink import SqlTelemetrySink
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import (
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    staffed_restaurant,
)
from tests.fakes import MemoryTelemetrySink

API = "/api/v1"
FAILING_URL = f"{API}/_prueba/falla"


@pytest.fixture
def sink(session: AsyncSession) -> SqlTelemetrySink:
    return SqlTelemetrySink(
        async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    )


@pytest.fixture
def telemetry(session: AsyncSession, sink: SqlTelemetrySink) -> Iterator[TelemetryRecorder]:
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


# --- Peticiones ---------------------------------------------------------------


async def test_una_peticion_se_guarda_con_su_plantilla_sus_tiempos_y_su_cuenta(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    local_a: StaffedRestaurant,
) -> None:
    response = await client.get(f"{API}/orders/987654", headers=authorization_for(local_a.admin))
    assert response.status_code == 404
    ok = await client.get(f"{API}/restaurant", headers=authorization_for(local_a.waiter))
    assert ok.status_code == 200

    await telemetry.flush()

    fallida, correcta = await _requests(session)
    assert fallida.route == "/api/v1/orders/{order_id}"
    assert fallida.method == "GET"
    assert fallida.status == 404
    assert fallida.request_id == response.headers[REQUEST_ID_HEADER]
    assert fallida.account_kind == "staff"
    assert fallida.restaurant_id == local_a.id
    assert fallida.account_id == local_a.admin.id
    # Al menos la lectura de la identidad y la del pedido.
    assert fallida.db_queries >= 2
    assert fallida.db_ms >= 0
    assert fallida.duration_ms >= fallida.db_ms
    assert correcta.route == "/api/v1/restaurant"
    assert correcta.status == 200
    assert correcta.account_id == local_a.waiter.id


async def test_la_cuenta_de_plataforma_y_la_anonima_se_distinguen(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    platform_admin: PlatformAdmin,
) -> None:
    await client.get(f"{API}/platform/auth/me", headers=_platform_headers(platform_admin))
    await client.get(f"{API}/menu")
    await client.get(f"{API}/no-existe/123")

    await telemetry.flush()

    plataforma, anonima, sin_ruta = await _requests(session)
    assert (plataforma.account_kind, plataforma.account_id, plataforma.restaurant_id) == (
        "platform",
        platform_admin.id,
        None,
    )
    assert plataforma.route == "/api/v1/platform/auth/me"
    assert (anonima.account_kind, anonima.status, anonima.account_id) == ("anonymous", 401, None)
    assert sin_ruta.route == UNMATCHED_ROUTE
    assert sin_ruta.status == 404


async def test_la_vista_previa_se_anota_como_tal(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    platform_admin: PlatformAdmin,
) -> None:
    muestra = await staffed_restaurant(session, "muestra", is_sandbox=True)
    token = TEST_TOKEN_SERVICE.issue_preview(
        muestra.waiter.id or 0, muestra.id, platform_admin_id=platform_admin.id or 0
    )

    response = await client.get(
        f"{API}/restaurant", headers={"Authorization": f"Bearer {token.value}"}
    )
    assert response.status_code == 200
    await telemetry.flush()

    (fila,) = await _requests(session)
    assert (fila.account_kind, fila.restaurant_id, fila.account_id) == (
        "preview",
        muestra.id,
        muestra.waiter.id,
    )


async def test_el_sondeo_los_avisos_y_el_panel_no_se_guardan(
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


async def test_los_4xx_dejan_un_aviso_con_el_request_id(
    client: AsyncClient, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    response = await client.get(f"{API}/menu")
    await telemetry.flush()

    (evento,) = await _events(session)
    assert evento.level == "warning"
    assert evento.logger == "resthub.http"
    assert evento.event == "request.completed"
    assert evento.request_id == response.headers[REQUEST_ID_HEADER]
    assert evento.traceback is None


async def test_un_error_no_controlado_queda_como_evento_con_traceback_y_su_peticion(
    app: FastAPI, client: AsyncClient, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    @app.get(FAILING_URL)
    async def falla() -> None:
        raise RuntimeError("se rompió la mesa 7")

    with pytest.raises(RuntimeError):
        await client.get(FAILING_URL, headers={REQUEST_ID_HEADER: "web-falla-000001"})
    await telemetry.flush()

    (peticion,) = await _requests(session)
    assert (peticion.route, peticion.status) == (FAILING_URL, 500)
    assert peticion.request_id == "web-falla-000001"
    (evento,) = await _events(session)
    assert (evento.level, evento.event) == ("error", "request.failed")
    assert evento.request_id == "web-falla-000001"
    assert evento.traceback is not None
    assert "RuntimeError: se rompió la mesa 7" in evento.traceback
    assert '"error_type": "RuntimeError"' in evento.fields
    assert "se rompió la mesa 7" in evento.fields


async def test_nunca_se_guardan_query_strings_ni_cabeceras(
    client: AsyncClient,
    session: AsyncSession,
    telemetry: TelemetryRecorder,
    local_a: StaffedRestaurant,
) -> None:
    headers = authorization_for(local_a.admin)
    await client.get(f"{API}/orders?status=secreto-en-la-url", headers=headers)
    await client.get(f"{API}/menu?token=secreto-en-la-url")
    await telemetry.flush()

    filas = [*(await _requests(session)), *(await _events(session))]
    assert len(filas) >= 3
    guardado = repr([vars(fila) for fila in filas])
    assert "secreto-en-la-url" not in guardado
    assert headers["Authorization"].removeprefix("Bearer ") not in guardado
    assert "?" not in guardado


# --- Eventos de log -------------------------------------------------------------


async def test_los_campos_sensibles_se_ocultan_a_cualquier_profundidad(
    app: FastAPI, session: AsyncSession, telemetry: TelemetryRecorder
) -> None:
    logger = get_logger("resthub.prueba")
    logger.warning(
        "prueba.sensible",
        password="hunter2",
        preview_code="483920",
        detalle={"Api_Key": "k-1", "interno": {"accessToken": "t-1"}, "mesa": 4},
        lista=[{"client_secret": "s-1"}],
        restaurant_id=9,
        mesa=4,
    )
    logger.info("prueba.informativa", mesa=4)
    await telemetry.flush()

    (evento,) = await _events(session)
    assert evento.event == "prueba.sensible"
    assert evento.logger == "resthub.prueba"
    assert evento.restaurant_id == 9
    for secreto in ("hunter2", "483920", "k-1", "t-1", "s-1"):
        assert secreto not in evento.fields
    detalle = _fields(evento)
    assert detalle["password"] == REDACTED
    assert detalle["preview_code"] == REDACTED
    assert detalle["detalle"] == {
        "Api_Key": REDACTED,
        "interno": {"accessToken": REDACTED},
        "mesa": 4,
    }
    assert detalle["lista"] == [{"client_secret": REDACTED}]
    assert detalle["mesa"] == 4


def test_la_regla_de_nombres_sensibles_compara_palabras_enteras() -> None:
    for name in ("password", "NEW_PASSWORD", "accessToken", "X-Auth-Token", "preview_code"):
        assert is_sensitive_key(name), name
    for name in ("jwt_secret_key", "authorization", "api_key", "apiKey", "tokens"):
        assert is_sensitive_key(name), name
    for name in ("monkey", "keyboard", "status", "user_id", "encoding", 7):
        assert not is_sensitive_key(name), name
    assert redact({"a": ({"token": 1},)}) == {"a": [{"token": REDACTED}]}


def test_el_traceback_largo_conserva_el_final() -> None:
    try:
        raise ValueError("x" * (MAX_TRACEBACK_LENGTH * 2))
    except ValueError as error:
        texto = format_traceback(error)

    assert len(texto) == MAX_TRACEBACK_LENGTH
    assert texto.startswith("…[recortado]")
    assert texto.endswith("x\n")


# --- Cola y escritura en segundo plano -------------------------------------------


async def test_con_la_cola_llena_se_descartan_los_mas_viejos_y_se_cuentan() -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(capacity=3)
    recorder.install(sink)
    ahora = datetime(2026, 9, 26, 12, tzinfo=UTC)

    for numero in range(5):
        recorder.record(_request(ahora, request_id=f"req-{numero:08d}"))
    assert (recorder.pending, recorder.dropped) == (3, 2)

    await recorder.flush()
    assert [record.request_id for record in sink.requests] == [
        "req-00000002",
        "req-00000003",
        "req-00000004",
    ]


async def test_un_lote_que_falla_se_reintenta_una_vez() -> None:
    sink = MemoryTelemetrySink(failures=1)
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    recorder.record(_request(datetime.now(UTC)))

    await recorder.flush()

    assert sink.attempts == 2
    assert len(sink.requests) == 1
    assert recorder.dropped == 0


async def test_un_lote_que_falla_dos_veces_se_descarta_y_se_cuenta() -> None:
    sink = MemoryTelemetrySink(failures=2)
    recorder = TelemetryRecorder(retry_delay=0)
    recorder.install(sink)
    recorder.record(_request(datetime.now(UTC)))
    recorder.record(EventRecord(at=datetime.now(UTC), level="warning", logger="x", event="prueba"))

    await recorder.flush()

    assert sink.attempts == 2
    assert sink.requests == []
    assert recorder.dropped == 2
    # El aviso de la falla no vuelve a la cola: no habría dónde escribirlo.
    assert recorder.pending == 0


async def test_los_lotes_son_de_a_lo_sumo_batch_size_filas() -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(batch_size=2)
    recorder.install(sink)
    for _ in range(5):
        recorder.record(_request(datetime.now(UTC)))

    await recorder.flush()

    assert sink.attempts == 3
    assert len(sink.requests) == 5


async def test_el_bucle_de_fondo_escribe_solo_y_al_apagar_vacia_la_cola() -> None:
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


async def test_la_purga_borra_lo_que_paso_la_retencion(
    sink: SqlTelemetrySink, session: AsyncSession
) -> None:
    ahora = datetime(2026, 9, 26, 12, tzinfo=UTC)
    viejo, reciente = ahora - timedelta(days=15), ahora - timedelta(days=13)
    await sink.write(
        [_request(viejo), _request(reciente)],
        [
            EventRecord(at=viejo, level="warning", logger="x", event="viejo"),
            EventRecord(at=reciente, level="warning", logger="x", event="reciente"),
        ],
    )
    recorder = TelemetryRecorder(retention_days=14)
    recorder.install(sink)

    assert await recorder.purge(now=ahora) == 2

    assert [fila.event for fila in await _events(session)] == ["reciente"]
    assert len(await _requests(session)) == 1


# --- Apagada ---------------------------------------------------------------------


async def test_apagada_no_captura_nada(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    sink = MemoryTelemetrySink()
    recorder = TelemetryRecorder(enabled=False)
    recorder.install(sink)
    set_telemetry(recorder)
    try:
        await client.get(f"{API}/restaurant", headers=authorization_for(local_a.admin))
        await client.get(f"{API}/menu")
        get_logger("resthub.prueba").error("prueba.apagada")
        await recorder.flush()
    finally:
        set_telemetry(None)

    assert (sink.requests, sink.events, sink.attempts) == ([], [], 0)
    assert not recorder.active


async def test_sin_sumidero_no_se_encola_nada(client: AsyncClient) -> None:
    recorder = TelemetryRecorder()
    set_telemetry(recorder)
    try:
        await client.get(f"{API}/menu")
    finally:
        set_telemetry(None)

    assert recorder.pending == 0


def test_la_configuracion_la_apaga(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_sin_ruta_resuelta_la_plantilla_es_fija() -> None:
    assert route_template({"type": "http", "path": "/x/1"}) == UNMATCHED_ROUTE


@pytest.fixture(autouse=True)
async def _sin_registro_ajeno() -> AsyncIterator[None]:
    # Ninguna prueba deja instalado su registro para la siguiente.
    yield
    set_telemetry(None)
