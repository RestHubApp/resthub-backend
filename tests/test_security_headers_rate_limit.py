"""Cabeceras de seguridad y límite general de peticiones por IP."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from resthub.core.config import get_settings
from resthub.core.rate_limit import RATE_LIMIT_DETAIL, RateLimitMiddleware
from resthub.core.security_headers import API_CSP, HSTS


async def test_cada_respuesta_del_api_lleva_las_cabeceras_de_seguridad(
    client: AsyncClient,
) -> None:
    respuesta = await client.get("/api/v1/health")
    sin_credencial = await client.get("/api/v1/auth/me")

    for r in (respuesta, sin_credencial):
        assert r.headers["strict-transport-security"] == HSTS
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["content-security-policy"] == API_CSP


async def test_la_documentacion_interactiva_no_lleva_la_csp_del_api(client: AsyncClient) -> None:
    # Swagger UI carga sus scripts de un CDN; con `default-src 'none'` no se vería.
    docs = await client.get("/api/v1/docs")

    assert docs.status_code == 200
    assert "content-security-policy" not in docs.headers
    assert docs.headers["x-content-type-options"] == "nosniff"


def _app(per_minute: int, reloj: list[float]) -> RateLimitMiddleware:
    app = FastAPI()

    @app.get("/api/v1/clientes")
    async def _clientes() -> dict[str, str]:
        return {"ok": "sí"}

    @app.get("/api/v1/health")
    async def _vida() -> dict[str, str]:
        return {"ok": "sí"}

    return RateLimitMiddleware(
        app, per_minute=per_minute, exempt=("/api/v1/health",), clock=lambda: reloj[0]
    )


async def test_pasado_el_limite_responde_429_hasta_la_ventana_siguiente() -> None:
    reloj = [0.0]
    transporte = ASGITransport(app=_app(3, reloj))
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        permitidas = [(await client.get("/api/v1/clientes")).status_code for _ in range(3)]
        reloj[0] = 20.0
        cortada = await client.get("/api/v1/clientes")
        vida = await client.get("/api/v1/health")
        reloj[0] = 61.0
        de_nuevo = await client.get("/api/v1/clientes")

    assert permitidas == [200, 200, 200]
    assert cortada.status_code == 429
    assert cortada.json()["detail"] == RATE_LIMIT_DETAIL
    assert cortada.headers["retry-after"] == "40"
    # El sondeo de vida no cuenta ni se corta.
    assert vida.status_code == 200
    assert de_nuevo.status_code == 200


async def test_cuenta_por_la_ip_que_agrega_el_proxy() -> None:
    # La que escribe el cliente al principio de X-Forwarded-For no cambia nada.
    reloj = [0.0]
    transporte = ASGITransport(app=_app(1, reloj))
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        a = await client.get("/api/v1/clientes", headers={"x-forwarded-for": "1.1.1.1, 9.9.9.9"})
        a_disfrazada = await client.get(
            "/api/v1/clientes", headers={"x-forwarded-for": "2.2.2.2, 9.9.9.9"}
        )
        b = await client.get("/api/v1/clientes", headers={"x-forwarded-for": "8.8.8.8"})

    assert [r.status_code for r in (a, a_disfrazada, b)] == [200, 429, 200]


async def test_en_cero_no_limita() -> None:
    transporte = ASGITransport(app=_app(0, [0.0]))
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        codigos = {(await client.get("/api/v1/clientes")).status_code for _ in range(20)}

    assert codigos == {200}


async def test_con_un_waf_por_delante_cuenta_la_ip_que_el_waf_informa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Detrás de Cloudflare, la última IP de X-Forwarded-For es la de Cloudflare.
    monkeypatch.setattr(get_settings(), "client_ip_header", "CF-Connecting-IP")
    transporte = ASGITransport(app=_app(1, [0.0]))
    cloudflare = "172.68.1.1"
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        ana = await client.get(
            "/api/v1/clientes",
            headers={"x-forwarded-for": cloudflare, "cf-connecting-ip": "1.1.1.1"},
        )
        luis = await client.get(
            "/api/v1/clientes",
            headers={"x-forwarded-for": cloudflare, "cf-connecting-ip": "8.8.8.8"},
        )
        ana_otra_vez = await client.get(
            "/api/v1/clientes",
            headers={"x-forwarded-for": cloudflare, "cf-connecting-ip": "1.1.1.1"},
        )

    assert [r.status_code for r in (ana, luis, ana_otra_vez)] == [200, 200, 429]
