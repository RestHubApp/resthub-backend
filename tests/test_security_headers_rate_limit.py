"""Cabeceras de seguridad y límite general de peticiones por IP."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from resthub.core.config import get_settings
from resthub.core.rate_limit import RATE_LIMIT_DETAIL, RateLimitMiddleware
from resthub.core.security_headers import API_CSP, HSTS
from tests.conftest import url


async def test_every_api_response_carries_security_headers(
    client: AsyncClient,
) -> None:
    response = await client.get(url("health"))
    no_credentials = await client.get(url("auth/me"))

    for r in (response, no_credentials):
        assert r.headers["strict-transport-security"] == HSTS
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["content-security-policy"] == API_CSP


async def test_interactive_docs_do_not_carry_the_api_csp(client: AsyncClient) -> None:
    # Swagger UI carga sus scripts de un CDN; con `default-src 'none'` no se vería.
    docs = await client.get(url("docs"))

    assert docs.status_code == 200
    assert "content-security-policy" not in docs.headers
    assert docs.headers["x-content-type-options"] == "nosniff"


def _app(per_minute: int, clock: list[float]) -> RateLimitMiddleware:
    app = FastAPI()

    @app.get(url("clientes"))
    async def _customers() -> dict[str, str]:
        return {"ok": "sí"}

    @app.get(url("health"))
    async def _liveness() -> dict[str, str]:
        return {"ok": "sí"}

    return RateLimitMiddleware(
        app, per_minute=per_minute, exempt=(url("health"),), clock=lambda: clock[0]
    )


async def test_over_the_limit_responds_429_until_the_next_window() -> None:
    clock = [0.0]
    transport = ASGITransport(app=_app(3, clock))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = [(await client.get(url("clientes"))).status_code for _ in range(3)]
        clock[0] = 20.0
        blocked = await client.get(url("clientes"))
        liveness = await client.get(url("health"))
        clock[0] = 61.0
        again = await client.get(url("clientes"))

    assert allowed == [200, 200, 200]
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == RATE_LIMIT_DETAIL
    assert blocked.headers["retry-after"] == "40"
    # El sondeo de vida no cuenta ni se corta.
    assert liveness.status_code == 200
    assert again.status_code == 200


async def test_counts_by_the_ip_the_proxy_appends() -> None:
    # La que escribe el cliente al principio de X-Forwarded-For no cambia nada.
    clock = [0.0]
    transport = ASGITransport(app=_app(1, clock))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await client.get(url("clientes"), headers={"x-forwarded-for": "1.1.1.1, 9.9.9.9"})
        a_disguised = await client.get(
            url("clientes"), headers={"x-forwarded-for": "2.2.2.2, 9.9.9.9"}
        )
        b = await client.get(url("clientes"), headers={"x-forwarded-for": "8.8.8.8"})

    assert [r.status_code for r in (a, a_disguised, b)] == [200, 429, 200]


async def test_zero_means_no_limit() -> None:
    transport = ASGITransport(app=_app(0, [0.0]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        codes = {(await client.get(url("clientes"))).status_code for _ in range(20)}

    assert codes == {200}


async def test_behind_a_waf_counts_the_ip_the_waf_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Detrás de Cloudflare, la última IP de X-Forwarded-For es la de Cloudflare.
    monkeypatch.setattr(get_settings(), "client_ip_header", "CF-Connecting-IP")
    transport = ASGITransport(app=_app(1, [0.0]))
    cloudflare = "172.68.1.1"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ana = await client.get(
            url("clientes"),
            headers={"x-forwarded-for": cloudflare, "cf-connecting-ip": "1.1.1.1"},
        )
        luis = await client.get(
            url("clientes"),
            headers={"x-forwarded-for": cloudflare, "cf-connecting-ip": "8.8.8.8"},
        )
        ana_again = await client.get(
            url("clientes"),
            headers={"x-forwarded-for": cloudflare, "cf-connecting-ip": "1.1.1.1"},
        )

    assert [r.status_code for r in (ana, luis, ana_again)] == [200, 200, 429]
