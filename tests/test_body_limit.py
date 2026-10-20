"""Tamaño máximo del cuerpo de una petición (413)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from resthub.core.body_limit import (
    BODY_TOO_LARGE_DETAIL,
    BodySizeLimitMiddleware,
    RequestBodyTooLargeError,
)
from resthub.core.config import get_settings
from resthub.core.error_codes import ErrorCode
from tests.conftest import url

LIMIT = get_settings().max_request_body_bytes


async def _chunks(total: int, size: int = 64 * 1024) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total:
        chunk = b"x" * min(size, total - sent)
        sent += len(chunk)
        yield chunk


def _assert_too_large(response_status: int, body: dict[str, str]) -> None:
    assert response_status == 413
    assert body == {"detail": BODY_TOO_LARGE_DETAIL, "code": ErrorCode.REQUEST_TOO_LARGE.value}


async def test_a_declared_length_over_the_limit_is_rejected_before_reading(
    client: AsyncClient,
) -> None:
    response = await client.post(
        url("auth/login"),
        content=b"x" * (LIMIT + 1),
        headers={"content-type": "application/json"},
    )

    _assert_too_large(response.status_code, response.json())
    # El 413 también lleva las cabeceras de seguridad.
    assert response.headers["cache-control"] == "no-store"


async def test_a_chunked_body_over_the_limit_is_cut_while_reading(client: AsyncClient) -> None:
    response = await client.post(
        url("auth/login"),
        content=_chunks(LIMIT + 1),
        headers={"content-type": "application/json"},
    )

    assert "content-length" not in response.request.headers
    _assert_too_large(response.status_code, response.json())


async def test_without_a_session_the_body_is_rejected_before_the_401(
    client: AsyncClient,
) -> None:
    declared = await client.post(
        url("customers"), content=b"x" * (LIMIT + 1), headers={"content-type": "application/json"}
    )
    chunked = await client.post(
        url("customers"), content=_chunks(LIMIT + 1), headers={"content-type": "application/json"}
    )

    _assert_too_large(declared.status_code, declared.json())
    _assert_too_large(chunked.status_code, chunked.json())


async def test_a_body_under_the_limit_reaches_the_route(client: AsyncClient) -> None:
    # Más grande que la foto de un plato en base64, por debajo del tope.
    padding = "x" * 700_000
    response = await client.post(
        url("auth/login"), json={"email": "ana@example.com", "password": padding}
    )

    assert response.status_code not in {413, 500}
    assert response.json()["code"] != ErrorCode.REQUEST_TOO_LARGE.value


async def _echo(scope: Scope, receive: Receive, send: Send) -> None:
    """Una app ASGI sin FastAPI: lee todo el cuerpo y responde cuántos bytes leyó."""
    assert scope["type"] == "http"
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


async def _answers_first(scope: Scope, receive: Receive, send: Send) -> None:
    """Empieza a responder antes de leer el cuerpo."""
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    while (await receive()).get("more_body", False):
        pass
    await send({"type": "http.response.body", "body": b"ok"})  # pragma: no cover - no llega


async def test_outside_fastapi_the_middleware_answers_the_413() -> None:
    transport = ASGITransport(app=BodySizeLimitMiddleware(_echo, max_bytes=1_024))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        over = await client.post("/", content=_chunks(2_048, size=512))
        under = await client.post("/", content=_chunks(1_024, size=512))
        exact = await client.post("/", content=b"x" * 1_024)
        no_body = await client.get("/")

    _assert_too_large(over.status_code, over.json())
    assert (under.status_code, under.text) == (200, "1024")
    assert (exact.status_code, exact.text) == (200, "1024")
    assert (no_body.status_code, no_body.text) == (200, "0")


async def test_once_the_response_started_the_error_is_not_hidden() -> None:
    transport = ASGITransport(app=BodySizeLimitMiddleware(_answers_first, max_bytes=1_024))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RequestBodyTooLargeError):
            await client.post("/", content=_chunks(2_048, size=512))


async def test_other_scopes_pass_through() -> None:
    seen: list[str] = []

    async def lifespan_app(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> dict[str, str]:  # pragma: no cover - no se llama
        return {"type": "lifespan.startup"}

    async def send(_message: object) -> None:  # pragma: no cover - no se llama
        return None

    await BodySizeLimitMiddleware(lifespan_app, max_bytes=1)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]
