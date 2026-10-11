"""Lo que hace el API cuando la base falla: hallazgos de los experimentos de caos.

Los experimentos (`chaos/experiments`) cortan PostgreSQL de verdad; estas
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
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.types import Message, Receive, Scope, Send

from resthub.core.database_errors import (
    DATABASE_UNAVAILABLE_DETAIL,
    RETRY_AFTER_SECONDS,
    is_database_unavailable,
)
from resthub.core.request_deadline import DEADLINE_DETAIL, RequestDeadlineMiddleware
from resthub.core.request_deadline import RETRY_AFTER_SECONDS as DEADLINE_RETRY_AFTER
from resthub.main import DEADLINE_EXEMPT_PATHS
from tests.builders import seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url


def _record_responses(app: FastAPI, events: list[str]) -> Any:
    async def wrapper(scope: Scope, receive: Receive, send: Send) -> None:
        async def send_and_track(message: Message) -> None:
            if message["type"] == "http.response.start":
                events.append("respuesta")
            await send(message)

        await app(scope, receive, send_and_track)

    return wrapper


async def test_order_is_committed_to_database_before_responding(
    app: FastAPI, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    # Hallazgo del experimento 01: el 201 salía antes del COMMIT. Si la base
    # caía en ese instante, el mesero veía el pedido creado y no quedaba nada.
    menu = await seed_menu(session, restaurant_a.id)
    events: list[str] = []
    event.listen(session.sync_session, "after_commit", lambda _: events.append("commit"))
    transport = ASGITransport(app=_record_responses(app, events))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            url("orders"),
            json={"type": "takeaway", "items": [{"menu_item_id": menu.lomo, "quantity": 1}]},
            headers=authorization_for(restaurant_a.waiter),
        )

    assert response.status_code == 201, response.text
    assert "commit" in events[: events.index("respuesta")], events


def _disconnection() -> DBAPIError:
    return DBAPIError(
        "SELECT 1",
        {},
        asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
        connection_invalidated=True,
    )


async def test_liveness_probe_checks_database(client: AsyncClient) -> None:
    response = await client.get(url("health"))

    assert response.status_code == 200
    assert response.json()["database"] == "ok"


async def test_liveness_probe_returns_503_without_database(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hallazgo del experimento 01: con PostgreSQL caído el sondeo decía «ok».
    async def no_database(*_: object, **__: object) -> None:
        raise ConnectionRefusedError(111, "Connect call failed")

    monkeypatch.setattr(type(session), "execute", no_database)

    response = await client.get(url("health"))

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "unavailable"


async def test_without_database_api_returns_503_with_message(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hallazgo del experimento 01: una conexión cortada salía como un 500 en
    # texto plano, sin nada que el frontend pudiera leer.
    async def database_down(*_: object, **__: object) -> None:
        raise _disconnection()

    monkeypatch.setattr(session, "execute", database_down)

    response = await client.get(
        url("orders/active"), headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 503
    assert response.json()["detail"] == DATABASE_UNAVAILABLE_DETAIL
    assert response.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    assert "Traceback" not in response.text


async def test_sql_error_remains_a_500(
    app: FastAPI,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Solo se traduce lo que dice que la base no está; un error del programa no
    # se disfraza de corte pasajero.
    async def broken_query(*_: object, **__: object) -> None:
        raise ProgrammingError("SELECT x", {}, Exception("column x does not exist"))

    monkeypatch.setattr(session, "execute", broken_query)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            url("orders/active"), headers=authorization_for(restaurant_a.waiter)
        )

    assert response.status_code == 500


@pytest.mark.parametrize(
    ("error", "unavailable"),
    [
        (_disconnection(), True),
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
def test_what_counts_as_database_unavailable(error: Exception, unavailable: bool) -> None:
    assert is_database_unavailable(error) is unavailable


def _slow_app(seconds: float) -> FastAPI:
    slow_app = FastAPI()

    @slow_app.get("/lenta")
    async def _slow() -> dict[str, str]:
        await asyncio.sleep(seconds)
        return {"ok": "sí"}

    @slow_app.get("/libre")
    async def _free() -> dict[str, str]:
        await asyncio.sleep(seconds)
        return {"ok": "sí"}

    return slow_app


async def test_request_past_deadline_gets_503_without_waiting_for_it_to_finish() -> None:
    # Hallazgo del experimento 02: con la base lenta, abrir un pedido tardaba
    # 54 s y el frontend ya se había rendido a los 15 s.
    app = RequestDeadlineMiddleware(_slow_app(5.0), seconds=0.2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = time.perf_counter()
        response = await client.get("/lenta")
        elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert response.json()["detail"] == DEADLINE_DETAIL
    assert response.headers["retry-after"] == str(DEADLINE_RETRY_AFTER)
    assert elapsed < 2.0


async def test_already_committed_work_is_not_cut_by_deadline() -> None:
    # Si el plazo vence después del COMMIT, un 503 diría «no terminó» de un
    # cobro ya guardado y reintentarlo lo registraría dos veces.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    saved_app = FastAPI()

    @saved_app.post("/cobro")
    async def _charge() -> dict[str, str]:
        async with AsyncSession(engine) as db:
            await db.execute(text("SELECT 1"))
            await db.commit()
        await asyncio.sleep(0.4)
        return {"ok": "sí"}

    app = RequestDeadlineMiddleware(saved_app, seconds=0.1)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/cobro")
    finally:
        await engine.dispose()

    assert response.status_code == 200


async def test_work_not_committed_is_cut() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    slow_app = FastAPI()

    @slow_app.post("/cobro")
    async def _charge() -> dict[str, str]:
        async with AsyncSession(engine) as db:
            await db.execute(text("SELECT 1"))
            await asyncio.sleep(5.0)
            await db.commit()
        return {"ok": "sí"}

    app = RequestDeadlineMiddleware(slow_app, seconds=0.1)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/cobro")
    finally:
        await engine.dispose()

    assert response.status_code == 503


async def test_exempt_routes_have_no_deadline() -> None:
    app = RequestDeadlineMiddleware(_slow_app(0.4), seconds=0.1, exempt=[r"/libre"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/libre")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        url("events"),
        url("billing/invoices"),
        url("billing/invoices/7/resend"),
        url("insights/restock/refresh"),
    ],
)
def test_what_waits_on_third_party_has_no_deadline(path: str) -> None:
    assert any(re.fullmatch(pattern, path) for pattern in DEADLINE_EXEMPT_PATHS)


def test_ordinary_route_has_deadline() -> None:
    assert not any(re.fullmatch(pattern, url("orders")) for pattern in DEADLINE_EXEMPT_PATHS)


async def test_liveness_probe_answers_in_time_with_slow_database(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hallazgo del experimento 02: con la base lenta el sondeo tardaba 5 s en
    # decir que no había base, porque además esperaba el rollback.
    async def slow_database(*_: object, **__: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(type(session), "execute", slow_database)
    started = time.perf_counter()
    response = await client.get(url("health"))

    assert response.status_code == 503
    assert time.perf_counter() - started < 3.0
