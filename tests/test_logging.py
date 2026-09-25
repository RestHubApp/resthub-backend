"""Registro de peticiones y protección de datos sensibles en los logs."""

from __future__ import annotations

from httpx import AsyncClient

from resthub.core.logs import REDACTED, mask_email, redact_sensitive
from resthub.core.request_logging import REQUEST_ID_HEADER

HEALTH = "/api/v1/health"


def test_redact_sensitive_hides_secrets_whatever_the_case() -> None:
    event = {"event": "x", "password": "secreta", "Authorization": "Bearer abc", "user_id": 7}

    result = redact_sensitive(None, "info", event)

    assert result["password"] == REDACTED
    assert result["Authorization"] == REDACTED
    assert result["user_id"] == 7


def test_mask_email_keeps_domain_and_first_letter() -> None:
    assert mask_email("juana.perez@example.com") == "j***@example.com"
    assert mask_email("sin-arroba") == REDACTED


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get(HEALTH)

    request_id = response.headers[REQUEST_ID_HEADER]
    assert len(request_id) == 32
    assert all(character in "0123456789abcdef" for character in request_id)


async def test_a_valid_incoming_request_id_is_kept(client: AsyncClient) -> None:
    response = await client.get(HEALTH, headers={REQUEST_ID_HEADER: "web-3f2a9c1d-0001"})

    assert response.headers[REQUEST_ID_HEADER] == "web-3f2a9c1d-0001"


async def test_a_malformed_incoming_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get(HEALTH, headers={REQUEST_ID_HEADER: "no\tvale"})

    assert response.headers[REQUEST_ID_HEADER] != "no\tvale"
    assert len(response.headers[REQUEST_ID_HEADER]) == 32
