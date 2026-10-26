"""Registro de peticiones y protección de datos sensibles en los logs."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from resthub.core.logs import REDACTED, configure_logging, get_logger, mask_email, redact_sensitive
from resthub.core.redaction import scrub_text
from resthub.core.request_logging import REQUEST_ID_HEADER
from tests.conftest import StaffedRestaurant, authorization_for, url

HEALTH = url("health")


def test_redact_sensitive_hides_secrets_whatever_the_case() -> None:
    event = {"event": "x", "password": "secreta", "Authorization": "Bearer abc", "user_id": 7}

    result = redact_sensitive(None, "info", event)

    assert result["password"] == REDACTED
    assert result["Authorization"] == REDACTED
    assert result["user_id"] == 7


def _fail(error: Exception) -> None:
    raise error


@pytest.fixture
def stdout_logs(capsys: pytest.CaptureFixture[str]) -> Iterator[pytest.CaptureFixture[str]]:
    """La captura de la salida, sin dejar un handler apuntando a ella al terminar."""
    yield capsys
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, logging.StreamHandler)]:
        if handler.stream is not sys.__stdout__ and handler.stream is not sys.__stderr__:
            root.removeHandler(handler)


def _json_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def test_stdout_line_masks_personal_data_and_tokens(
    stdout_logs: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", json=True)

    get_logger("resthub.test").warning(
        "aviso a juana.perez@example.com",
        customer_phone="987654321",
        dni="45678912",
        contact="escribir a ana@correo.pe o al +51 987 654 321",
        note="DNI 45678912 con Bearer abc.def",
        nested={"api_token": "t-123", "detail": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.firma"},
        order_id=12345678,
    )

    raw = stdout_logs.readouterr().out
    for secret in (
        "juana.perez@example.com",
        "ana@correo.pe",
        "987654321",
        "987 654 321",
        "45678912",
        "abc.def",
        "t-123",
        "eyJhbGciOiJIUzI1NiJ9",
    ):
        assert secret not in raw
    (line,) = [json.loads(text) for text in raw.splitlines() if text]
    assert line["event"] == "aviso a j***@example.com"
    assert line["customer_phone"] == REDACTED
    assert line["dni"] == REDACTED
    assert line["contact"] == f"escribir a a***@correo.pe o al {REDACTED}"
    assert line["nested"] == {"api_token": REDACTED, "detail": REDACTED}
    # Un entero no es un texto: los identificadores se ven tal cual.
    assert line["order_id"] == 12345678


def test_stdout_line_scrubs_library_messages(stdout_logs: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json=True)

    logging.getLogger("resthub.test.library").warning("conexión de %s", "ana@correo.pe")

    (line,) = _json_lines(stdout_logs)
    assert line["event"] == "conexión de a***@correo.pe"


def test_stdout_exception_is_scrubbed_and_has_no_locals(
    stdout_logs: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", json=True)
    customer_note = "nota-privada-de-la-mesa"
    message = f"falló para juan@x.com con 45678912 ({len(customer_note)})"

    try:
        _fail(ValueError(message))
    except ValueError:
        get_logger("resthub.test").exception("test.failed")

    raw = stdout_logs.readouterr().out
    assert "juan@x.com" not in raw
    assert "45678912" not in raw
    assert "nota-privada-de-la-mesa" not in raw
    assert "j***@x.com" in raw
    assert "ValueError" in raw


def test_console_exception_is_scrubbed(stdout_logs: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json=False)
    message = "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.firma de juan@x.com"

    try:
        _fail(RuntimeError(message))
    except RuntimeError:
        get_logger("resthub.test").exception("test.failed")

    raw = stdout_logs.readouterr().out
    assert "eyJhbGciOiJIUzI1NiJ9" not in raw
    assert "juan@x.com" not in raw
    assert "RuntimeError" in raw


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-20T10:00:00+00:00",
        "200.48.1.10",
        "F001-00000123",
        "total 12345678.50",
        "/orders/123456789",
        "request web-3f2a9c1d-0001 y a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "1234567 platos",
        "j***@example.com",
    ],
)
def test_scrub_text_leaves_non_personal_numbers_alone(text: str) -> None:
    assert scrub_text(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GET /lookup/dni/45678912", f"GET /lookup/dni/{REDACTED}"),
        ("/lookup/ruc/20123456789?x=1", f"/lookup/ruc/{REDACTED}?x=1"),
        ("/customers?q=987654321", f"/customers?q={REDACTED}"),
    ],
)
def test_scrub_text_hides_documents_and_phones_in_paths(text: str, expected: str) -> None:
    assert scrub_text(text) == expected


def test_scrub_text_is_idempotent() -> None:
    text = "ana@correo.pe, +51 987 654 321, RUC 20123456789"

    once = scrub_text(text)

    assert once == f"a***@correo.pe, {REDACTED}, RUC {REDACTED}"
    assert scrub_text(once) == once


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


async def test_request_lines_carry_account_and_client_network(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    stdout_logs: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", json=True)
    stdout_logs.readouterr()

    response = await client.get(
        url("auth/me"),
        headers={
            **authorization_for(restaurant_a.admin),
            "x-forwarded-for": "10.0.0.1, 200.48.1.7",
        },
    )

    assert response.status_code == 200
    completed = [line for line in _json_lines(stdout_logs) if line["event"] == "request.completed"]
    assert completed[-1]["account_id"] == restaurant_a.admin.id
    assert completed[-1]["restaurant_id"] == restaurant_a.id
    assert completed[-1]["account_kind"] == "staff"
    # La IP de los límites (la última de X-Forwarded-For), sin su último bloque.
    assert completed[-1]["client_network"] == "200.48.1.x"


async def test_anonymous_request_lines_carry_only_the_network(
    client: AsyncClient, stdout_logs: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", json=True)
    stdout_logs.readouterr()

    await client.get(url("auth/me"), headers={"x-forwarded-for": "2001:db8:abcd:12::1"})

    (line,) = [line for line in _json_lines(stdout_logs) if line["event"] == "request.completed"]
    assert line["client_network"] == "2001:db8:abcd::/48"
    assert "account_id" not in line
