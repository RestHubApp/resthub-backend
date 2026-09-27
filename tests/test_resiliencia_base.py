"""Lo que hace el API cuando la base falla: hallazgos de los experimentos de caos.

Los experimentos (`chaos/experimentos`) cortan PostgreSQL de verdad; estas
pruebas fijan el comportamiento corregido sin necesitar PostgreSQL.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message, Receive, Scope, Send

from resthub.core.database_errors import (
    DATABASE_UNAVAILABLE_DETAIL,
    RETRY_AFTER_SECONDS,
    is_database_unavailable,
)
from resthub.core.request_deadline import DEADLINE_DETAIL, RequestDeadlineMiddleware
from resthub.core.request_deadline import RETRY_AFTER_SECONDS as DEADLINE_RETRY_AFTER
from resthub.main import DEADLINE_EXEMPT_PATHS
from tests.builders import carta
from tests.conftest import StaffedRestaurant, authorization_for


def _anota_respuestas(app: FastAPI, eventos: list[str]) -> Any:
    async def envoltorio(scope: Scope, receive: Receive, send: Send) -> None:
        async def enviar(message: Message) -> None:
            if message["type"] == "http.response.start":
                eventos.append("respuesta")
            await send(message)

        await app(scope, receive, enviar)

    return envoltorio


async def test_el_pedido_se_confirma_en_la_base_antes_de_responder(
    app: FastAPI, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    # Hallazgo del experimento 01: el 201 salía antes del COMMIT. Si la base
    # caía en ese instante, el mesero veía el pedido creado y no quedaba nada.
    menu = await carta(session, local_a.id)
    eventos: list[str] = []
    event.listen(session.sync_session, "after_commit", lambda _: eventos.append("commit"))
    transport = ASGITransport(app=_anota_respuestas(app, eventos))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/orders",
            json={"type": "takeaway", "items": [{"menu_item_id": menu.lomo, "quantity": 1}]},
            headers=authorization_for(local_a.waiter),
        )

    assert response.status_code == 201, response.text
    assert "commit" in eventos[: eventos.index("respuesta")], eventos


def _desconexion() -> DBAPIError:
    return DBAPIError(
        "SELECT 1",
        {},
        asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
        connection_invalidated=True,
    )


async def test_el_sondeo_de_vida_mira_la_base(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["database"] == "ok"


async def test_el_sondeo_de_vida_responde_503_sin_base(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hallazgo del experimento 01: con PostgreSQL caído el sondeo decía «ok».
    async def sin_base(*_: object, **__: object) -> None:
        raise ConnectionRefusedError(111, "Connect call failed")

    monkeypatch.setattr(session, "execute", sin_base)

    response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "unavailable"


async def test_sin_base_el_api_responde_503_con_mensaje(
    client: AsyncClient,
    session: AsyncSession,
    local_a: StaffedRestaurant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hallazgo del experimento 01: una conexión cortada salía como un 500 en
    # texto plano, sin nada que el frontend pudiera leer.
    async def base_caida(*_: object, **__: object) -> None:
        raise _desconexion()

    monkeypatch.setattr(session, "execute", base_caida)

    response = await client.get("/api/v1/orders/active", headers=authorization_for(local_a.waiter))

    assert response.status_code == 503
    assert response.json()["detail"] == DATABASE_UNAVAILABLE_DETAIL
    assert response.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    assert "Traceback" not in response.text


async def test_un_error_de_sql_sigue_siendo_un_500(
    app: FastAPI,
    session: AsyncSession,
    local_a: StaffedRestaurant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Solo se traduce lo que dice que la base no está; un error del programa no
    # se disfraza de corte pasajero.
    async def consulta_rota(*_: object, **__: object) -> None:
        raise ProgrammingError("SELECT x", {}, Exception("column x does not exist"))

    monkeypatch.setattr(session, "execute", consulta_rota)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/orders/active", headers=authorization_for(local_a.waiter)
        )

    assert response.status_code == 500


@pytest.mark.parametrize(
    ("error", "no_disponible"),
    [
        (_desconexion(), True),
        (PoolTimeoutError("QueuePool limit of size 10 overflow 5 reached"), True),
        (
            DBAPIError("SELECT 1", {}, asyncpg.CannotConnectNowError("the database is starting")),
            True,
        ),
        (IntegrityError("INSERT", {}, Exception("duplicate key")), False),
        # Un `OSError` que no salió del driver de la base no es un corte de la base.
        (ConnectionRefusedError(111, "otro servicio"), False),
    ],
)
def test_que_cuenta_como_base_no_disponible(error: Exception, no_disponible: bool) -> None:
    assert is_database_unavailable(error) is no_disponible


def _app_lenta(segundos: float) -> FastAPI:
    lenta = FastAPI()

    @lenta.get("/lenta")
    async def _lenta() -> dict[str, str]:
        await asyncio.sleep(segundos)
        return {"ok": "sí"}

    @lenta.get("/libre")
    async def _libre() -> dict[str, str]:
        await asyncio.sleep(segundos)
        return {"ok": "sí"}

    return lenta


async def test_una_peticion_que_pasa_el_plazo_recibe_503_sin_esperar_a_que_termine() -> None:
    # Hallazgo del experimento 02: con la base lenta, abrir un pedido tardaba
    # 54 s y el frontend ya se había rendido a los 15 s.
    app = RequestDeadlineMiddleware(_app_lenta(5.0), seconds=0.2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        inicio = time.perf_counter()
        response = await client.get("/lenta")
        transcurrido = time.perf_counter() - inicio

    assert response.status_code == 503
    assert response.json()["detail"] == DEADLINE_DETAIL
    assert response.headers["retry-after"] == str(DEADLINE_RETRY_AFTER)
    assert transcurrido < 2.0


async def test_las_rutas_exentas_no_tienen_plazo() -> None:
    app = RequestDeadlineMiddleware(_app_lenta(0.4), seconds=0.1, exempt=[r"/libre"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/libre")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "ruta",
    [
        "/api/v1/events",
        "/api/v1/billing/invoices",
        "/api/v1/billing/invoices/7/resend",
        "/api/v1/insights/restock/refresh",
    ],
)
def test_lo_que_espera_a_un_tercero_queda_sin_plazo(ruta: str) -> None:
    assert any(re.fullmatch(patron, ruta) for patron in DEADLINE_EXEMPT_PATHS)


def test_una_ruta_comun_tiene_plazo() -> None:
    assert not any(re.fullmatch(patron, "/api/v1/orders") for patron in DEADLINE_EXEMPT_PATHS)


async def test_el_sondeo_de_vida_contesta_a_tiempo_con_la_base_lenta(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hallazgo del experimento 02: con la base lenta el sondeo tardaba 5 s en
    # decir que no había base, porque además esperaba el rollback.
    async def base_lenta(*_: object, **__: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(session, "execute", base_lenta)
    inicio = time.perf_counter()
    response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert time.perf_counter() - inicio < 3.0
