"""Compresión gzip de las respuestas: solo `GET`, nunca el acceso ni lo ya comprimido."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.compression import MINIMUM_SIZE_BYTES, CompressionMiddleware, accepts_gzip
from resthub.main import UNCOMPRESSED_PATHS
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from tests.conftest import StaffedRestaurant, authorization_for, build_user, url

EXCLUDED = (url("auth"), url("platform/auth"))
GZIP = {"Accept-Encoding": "gzip"}
BIG = MINIMUM_SIZE_BYTES * 4
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
CONTENT_TYPES = (
    "application/json",
    "text/plain; charset=utf-8",
    "text/event-stream",
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "application/zip",
)
COMPRESSIBLE_TYPES = {"application/json", "text/plain; charset=utf-8"}


def _echo_app() -> FastAPI:
    """Responde en cualquier ruta y método un cuerpo del tamaño y tipo que se le pida."""
    app = FastAPI()

    @app.api_route("/{path:path}", methods=list(METHODS))
    async def _echo(request: Request) -> Response:
        size = int(request.query_params.get("size", "0"))
        media_type = request.query_params.get("type", "application/json")
        return Response(b"a" * size, media_type=media_type)

    app.add_middleware(CompressionMiddleware, excluded_prefixes=EXCLUDED)
    return app


async def _request(
    method: str,
    path: str,
    *,
    size: int,
    media_type: str = "application/json",
    accept_encoding: str = "gzip",
) -> Response:
    async with AsyncClient(transport=ASGITransport(app=_echo_app()), base_url="http://t") as c:
        response = await c.request(
            method,
            path,
            params={"size": size, "type": media_type},
            headers={"Accept-Encoding": accept_encoding},
        )
    return Response(
        response.content, status_code=response.status_code, headers=dict(response.headers)
    )


def _is_gzip(response: Response) -> bool:
    return response.headers.get("content-encoding") == "gzip"


async def test_a_large_get_is_compressed_and_arrives_intact() -> None:
    response = await _request("GET", url("menu"), size=BIG)

    assert _is_gzip(response)
    assert "Accept-Encoding" in response.headers["vary"]
    assert response.body == b"a" * BIG


async def test_a_small_get_goes_as_is() -> None:
    response = await _request("GET", url("menu"), size=MINIMUM_SIZE_BYTES - 1)

    assert not _is_gzip(response)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_only_get_is_compressed(method: str) -> None:
    # Los secretos que entrega el API (contraseña temporal, códigos de recuperación,
    # enlace de vista previa) salen de peticiones que no son GET: comprimirlos abre BREACH.
    response = await _request(method, url("staff"), size=BIG)

    assert not _is_gzip(response)


@pytest.mark.parametrize(
    "path",
    [url("auth"), url("auth/me"), url("platform/auth/me"), url("auth/me/sessions")],
)
async def test_the_sign_in_routes_are_never_compressed(path: str) -> None:
    response = await _request("GET", path, size=BIG)

    assert not _is_gzip(response)


@pytest.mark.parametrize("path", [url("authors"), url("platform/authority")])
async def test_an_excluded_prefix_is_a_whole_segment(path: str) -> None:
    response = await _request("GET", path, size=BIG)

    assert _is_gzip(response)


@pytest.mark.parametrize("media_type", sorted(set(CONTENT_TYPES) - COMPRESSIBLE_TYPES))
async def test_already_compressed_files_and_the_event_stream_go_as_is(media_type: str) -> None:
    response = await _request("GET", url("billing/invoices/1/pdf"), size=BIG, media_type=media_type)

    assert not _is_gzip(response)


@pytest.mark.parametrize("accept_encoding", ["identity", "gzip;q=0, identity", "br", "gzip; q=0.0"])
async def test_a_client_that_does_not_accept_gzip_gets_it_plain(accept_encoding: str) -> None:
    # Starlette solo busca «gzip» en la cabecera: `gzip;q=0` (lo rechaza) recibía gzip.
    response = await _request("GET", url("menu"), size=BIG, accept_encoding=accept_encoding)

    assert not _is_gzip(response)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("gzip", True),
        ("GZIP", True),
        ("deflate, gzip;q=0.5", True),
        ("x-gzip", True),
        ("*", True),
        ("*;q=0", False),
        ("gzip;q=0, *", False),
        ("*, gzip;q=0", False),
        ("gzip;q=0", False),
        ("gzip;q=0.000", False),
        ("gzip;q=abc", False),
        ("identity", False),
        ("", False),
        (" , ;", False),
    ],
)
def test_accepts_gzip_reads_the_weights(header: str, *, expected: bool) -> None:
    assert accepts_gzip(header) is expected


@given(
    gzip_weight=st.none() | st.sampled_from(["0", "0.0", "0.001", "0.5", "1", "1.0"]),
    others=st.lists(st.sampled_from(["br", "deflate", "identity", "zstd"]), max_size=3),
)
def test_accepts_gzip_follows_the_explicit_gzip_weight(
    gzip_weight: str | None, others: list[str]
) -> None:
    # Sin comodín, gzip se acepta solo si se nombra y su peso es mayor que cero.
    parts = list(others)
    if gzip_weight is not None:
        parts.append(f"gzip;q={gzip_weight}")
    header = ", ".join(parts)

    expected = gzip_weight is not None and float(gzip_weight) > 0
    assert accepts_gzip(header) is expected


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    method=st.sampled_from(METHODS),
    path=st.sampled_from(
        [
            url("menu"),
            url("auth"),
            url("auth/me"),
            url("authx"),
            url("platform/auth/login"),
            url("platform/restaurants"),
            url("insights/summary"),
        ]
    ),
    size=st.integers(min_value=0, max_value=MINIMUM_SIZE_BYTES * 3),
    media_type=st.sampled_from(CONTENT_TYPES),
    accept_encoding=st.sampled_from(
        ["gzip", "identity", "gzip;q=0, identity", "br, gzip;q=0.5", "deflate, br"]
    ),
)
async def test_compression_follows_the_policy(
    method: str, path: str, size: int, media_type: str, accept_encoding: str
) -> None:
    # El oráculo sale de la política escrita en core/compression.py, no del código.
    in_excluded_route = any(path == p or path.startswith(p + "/") for p in EXCLUDED)
    client_accepts = accept_encoding in {"gzip", "br, gzip;q=0.5"}
    expected = (
        client_accepts
        and method == "GET"
        and not in_excluded_route
        and size >= MINIMUM_SIZE_BYTES
        and media_type in COMPRESSIBLE_TYPES
    )

    response = await _request(
        method, path, size=size, media_type=media_type, accept_encoding=accept_encoding
    )

    assert _is_gzip(response) is expected
    assert response.body == b"a" * size


def test_the_app_installs_compression_excluding_both_sign_in_routes(app: FastAPI) -> None:
    installed = [m for m in app.user_middleware if m.cls is CompressionMiddleware]

    assert len(installed) == 1
    assert installed[0].kwargs["excluded_prefixes"] == UNCOMPRESSED_PATHS == EXCLUDED


async def test_the_real_api_compresses_a_large_get_and_keeps_its_security_headers(
    client: AsyncClient,
) -> None:
    # El esquema OpenAPI es un GET grande que se publica con DEBUG=true (el de las pruebas).
    response = await client.get(url("openapi.json"), headers=GZIP)

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["openapi"]


async def test_the_real_api_does_not_compress_a_large_session_check(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    # Un nombre largo lleva `/auth/me`, que trae el `csrf_token`, por encima del umbral:
    # sin la exclusión saldría con gzip (BREACH).
    long_name = "Ñañez " * 20
    user = await SqlAlchemyUserRepository(session).add(
        build_user(restaurant_a.id, "largo@local-a.pe", restaurant_a.admin.role, long_name.strip())
    )
    await session.commit()

    response = await client.get(url("auth/me"), headers={**GZIP, **authorization_for(user)})

    assert response.status_code == 200
    assert len(response.content) >= MINIMUM_SIZE_BYTES
    assert "content-encoding" not in response.headers
