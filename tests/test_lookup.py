"""Consulta de RUC y DNI: formato, adaptador de Factiliza, memoria, límite y HTTP.

Ninguna prueba llama a Factiliza: el adaptador corre contra un transporte HTTP falso con
las respuestas que documenta https://docs.factiliza.com/api-consulta.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.error_codes import ErrorCode
from resthub.core.logs import configure_logging
from resthub.core.permissions import Permission
from resthub.modules.lookup.adapters.api.dependencies import (
    get_document_lookup,
    get_lookup_quota,
)
from resthub.modules.lookup.adapters.factiliza.client import FactilizaDocumentLookup
from resthub.modules.lookup.adapters.memory.cache import MAX_ENTRIES, CachedDocumentLookup
from resthub.modules.lookup.adapters.memory.quota import PerAccountLookupQuota
from resthub.modules.lookup.domain.documents import (
    DocumentRecord,
    DocumentType,
    ensure_valid_document,
    is_valid_dni,
    is_valid_ruc,
)
from resthub.modules.lookup.domain.exceptions import (
    DocumentNotFoundError,
    InvalidDocumentNumberError,
    LookupDisabledError,
    LookupRateLimitedError,
    LookupUnavailableError,
)
from resthub.modules.lookup.use_cases.lookup_document import LookupDocument
from tests.conftest import StaffedRestaurant, authorization_for, staff_with_role, url

VALID_RUC = "20552103816"
VALID_DNI = "27427864"
TOKEN = "token-de-prueba-que-no-debe-salir-en-logs"

RUC_BODY = {
    "status": 200,
    "success": True,
    "message": "Exito",
    "data": {
        "numero": VALID_RUC,
        "nombre_o_razon_social": "AGROLIGHT PERU S.A.C.",
        "tipo_contribuyente": "SOCIEDAD ANONIMA CERRADA",
        "estado": "SUSPENSION TEMPORAL",
        "condicion": "HABIDO",
        "direccion": "PJ. JORGE BASADRE NRO. 158",
        "direccion_completa": "PJ. JORGE BASADRE NRO. 158, LIMA - LIMA - SANTA ANITA",
    },
}
DNI_BODY = {
    "status": 200,
    "success": True,
    "message": "Exito",
    "data": {
        "numero": VALID_DNI,
        "nombres": "JOSE PEDRO",
        "apellido_paterno": "CASTILLO",
        "apellido_materno": "TERRONES",
        "nombre_completo": "CASTILLO TERRONES, JOSE PEDRO",
        "direccion": "CASERIO PUÑA",
        "direccion_completa": "CASERIO PUÑA, CAJAMARCA - CHOTA - TACABAMBA",
    },
}


def _record(number: str = VALID_RUC) -> DocumentRecord:
    return DocumentRecord(
        DocumentType.RUC, number, "AGROLIGHT PERU S.A.C.", "Lima", "ACTIVO", "HABIDO"
    )


class FakeLookup:
    """Proveedor de prueba: responde lo configurado y anota cuántas veces lo llamaron."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[DocumentType, str]] = []
        self.error: Exception | None = None

    async def lookup(self, document_type: DocumentType, number: str) -> DocumentRecord:
        self.calls.append((document_type, number))
        if self.error is not None:
            raise self.error
        return DocumentRecord(document_type, number, "NOMBRE DE PRUEBA", "Av. Lima 123", None, None)


class Ticks:
    """Reloj monotónico controlable."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# -- Formato ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [VALID_RUC, "10456789019"])
def test_valid_ruc_passes_the_check_digit(value: str) -> None:
    assert is_valid_ruc(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "2055210381",
        "205521038160",
        "20552103817",
        "30552103816",
        "2055210381a",
        "".join(chr(0xFF10 + int(digit)) for digit in VALID_RUC),  # ancho completo
    ],
)
def test_invalid_ruc_formats_are_rejected(value: str) -> None:
    assert not is_valid_ruc(value)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("27427864", True),
        ("2742786", False),
        ("274278644", False),
        ("2742786a", False),
        ("", False),
    ],
)
def test_dni_needs_exactly_eight_digits(value: str, valid: bool) -> None:
    assert is_valid_dni(value) is valid


def test_ensure_valid_document_trims_and_explains() -> None:
    assert ensure_valid_document(DocumentType.DNI, " 27427864 ") == VALID_DNI
    with pytest.raises(InvalidDocumentNumberError, match="RUC"):
        ensure_valid_document(DocumentType.RUC, "123")
    with pytest.raises(InvalidDocumentNumberError, match="DNI"):
        ensure_valid_document(DocumentType.DNI, "123")


# -- Adaptador de Factiliza ---------------------------------------------------------------------


def _adapter(handler: Any, token: str = TOKEN) -> FactilizaDocumentLookup:
    return FactilizaDocumentLookup(
        token=token,
        base_url="https://factiliza.test/v1/",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )


async def test_factiliza_looks_up_a_ruc_with_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RUC_BODY)

    record = await _adapter(handler).lookup(DocumentType.RUC, VALID_RUC)

    assert str(seen[0].url) == f"https://factiliza.test/v1/ruc/info/{VALID_RUC}"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert record == DocumentRecord(
        DocumentType.RUC,
        VALID_RUC,
        "AGROLIGHT PERU S.A.C.",
        "PJ. JORGE BASADRE NRO. 158, LIMA - LIMA - SANTA ANITA",
        "SUSPENSION TEMPORAL",
        "HABIDO",
    )


async def test_factiliza_looks_up_a_dni_and_builds_the_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DNI_BODY)

    record = await _adapter(handler).lookup(DocumentType.DNI, VALID_DNI)

    assert str(seen[0].url).endswith(f"/dni/info/{VALID_DNI}")
    assert record.name == "JOSE PEDRO CASTILLO TERRONES"
    assert record.address == "CASERIO PUÑA, CAJAMARCA - CHOTA - TACABAMBA"
    assert record.status is None
    assert record.condition is None


async def test_factiliza_dni_falls_back_to_full_name_and_plain_address() -> None:
    body = {"success": True, "data": {"nombre_completo": "CASTILLO, JOSE", "direccion": "CALLE 1"}}

    record = await _adapter(lambda _: httpx.Response(200, json=body)).lookup(
        DocumentType.DNI, VALID_DNI
    )

    assert record.name == "CASTILLO, JOSE"
    assert record.address == "CALLE 1"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"status": 400, "success": False, "message": "Bad Request"}),
        httpx.Response(404, json={}),
        httpx.Response(200, json={"success": False, "message": "No encontrado"}),
        httpx.Response(200, json={"success": True, "data": None}),
        httpx.Response(200, json={"success": True, "data": {"numero": VALID_RUC}}),
        httpx.Response(200, json=["no es un objeto"]),
    ],
)
async def test_factiliza_not_found_variants(response: httpx.Response) -> None:
    with pytest.raises(DocumentNotFoundError):
        await _adapter(lambda _: response).lookup(DocumentType.RUC, VALID_RUC)


@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
async def test_factiliza_provider_errors_are_unavailable(
    status_code: int, caplog: pytest.LogCaptureFixture
) -> None:
    configure_logging(level="DEBUG", json=False)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LookupUnavailableError):
        await _adapter(lambda _: httpx.Response(status_code, json={})).lookup(
            DocumentType.RUC, VALID_RUC
        )

    assert TOKEN not in caplog.text
    assert VALID_RUC not in caplog.text


async def test_factiliza_timeout_and_garbage_are_unavailable() -> None:
    def times_out(request: httpx.Request) -> httpx.Response:
        message = "lento"
        raise httpx.ReadTimeout(message, request=request)

    with pytest.raises(LookupUnavailableError):
        await _adapter(times_out).lookup(DocumentType.RUC, VALID_RUC)
    with pytest.raises(LookupUnavailableError):
        await _adapter(lambda _: httpx.Response(200, text="<html>")).lookup(
            DocumentType.RUC, VALID_RUC
        )


def test_factiliza_is_disabled_without_token() -> None:
    assert not _adapter(lambda _: httpx.Response(200), token="").enabled
    assert _adapter(lambda _: httpx.Response(200)).enabled


# -- Memoria y límite ---------------------------------------------------------------------------


async def test_cache_answers_repeated_lookups_until_it_expires() -> None:
    inner, ticks = FakeLookup(), Ticks()
    cached = CachedDocumentLookup(inner, ttl_seconds=60, clock=ticks)

    first = await cached.lookup(DocumentType.RUC, VALID_RUC)
    again = await cached.lookup(DocumentType.RUC, VALID_RUC)
    ticks.now += 61
    await cached.lookup(DocumentType.RUC, VALID_RUC)

    assert first == again
    assert len(inner.calls) == 2
    assert cached.enabled


async def test_cache_does_not_keep_failures_nor_when_ttl_is_zero() -> None:
    inner = FakeLookup()
    cached = CachedDocumentLookup(inner, ttl_seconds=60)
    inner.error = DocumentNotFoundError()
    with pytest.raises(DocumentNotFoundError):
        await cached.lookup(DocumentType.DNI, VALID_DNI)
    inner.error = None
    await cached.lookup(DocumentType.DNI, VALID_DNI)
    await cached.lookup(DocumentType.DNI, VALID_DNI)
    off = CachedDocumentLookup(inner, ttl_seconds=0)
    await off.lookup(DocumentType.DNI, VALID_DNI)
    await off.lookup(DocumentType.DNI, VALID_DNI)

    assert len(inner.calls) == 4


async def test_cache_is_bounded() -> None:
    inner = FakeLookup()
    cached = CachedDocumentLookup(inner, ttl_seconds=600)

    for index in range(MAX_ENTRIES + 10):
        await cached.lookup(DocumentType.DNI, f"{index:08d}")

    assert len(cached._entries) == MAX_ENTRIES


def test_quota_limits_each_account_per_minute() -> None:
    ticks = Ticks()
    quota = PerAccountLookupQuota(per_minute=2, clock=ticks)

    allowed = [quota.acquire(1), quota.acquire(1)]
    blocked = quota.acquire(1)
    other_account = quota.acquire(2)
    ticks.now += 61

    assert allowed == [0, 0]
    assert 1 <= blocked <= 60
    assert other_account == 0
    assert quota.acquire(1) == 0


def test_quota_zero_means_unlimited() -> None:
    quota = PerAccountLookupQuota(per_minute=0)

    assert all(quota.acquire(1) == 0 for _ in range(100))


# -- Caso de uso ------------------------------------------------------------------------------


async def test_use_case_checks_format_and_quota_before_calling_the_provider() -> None:
    gateway = FakeLookup()
    lookup = LookupDocument(gateway, PerAccountLookupQuota(per_minute=1))

    with pytest.raises(InvalidDocumentNumberError):
        await lookup(1, DocumentType.RUC, "123")
    await lookup(1, DocumentType.DNI, VALID_DNI)
    with pytest.raises(LookupRateLimitedError) as limited:
        await lookup(1, DocumentType.DNI, VALID_DNI)

    assert limited.value.retry_after_seconds >= 1
    assert len(gateway.calls) == 1


async def test_use_case_refuses_when_disabled() -> None:
    lookup = LookupDocument(FakeLookup(enabled=False), PerAccountLookupQuota(per_minute=5))

    with pytest.raises(LookupDisabledError):
        await lookup(1, DocumentType.DNI, VALID_DNI)


# -- HTTP -------------------------------------------------------------------------------------


@pytest.fixture
def lookup_gateway(app: FastAPI) -> FakeLookup:
    gateway = FakeLookup()
    app.dependency_overrides[get_document_lookup] = lambda: gateway
    quota = PerAccountLookupQuota(per_minute=3)
    app.dependency_overrides[get_lookup_quota] = lambda: quota
    return gateway


async def test_ruc_lookup_returns_the_record(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lookup_gateway: FakeLookup
) -> None:
    response = await client.get(
        url(f"lookup/ruc/{VALID_RUC}"), headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 200
    assert response.json() == {
        "document_type": "ruc",
        "document_number": VALID_RUC,
        "name": "NOMBRE DE PRUEBA",
        "address": "Av. Lima 123",
        "status": None,
        "condition": None,
    }
    assert lookup_gateway.calls == [(DocumentType.RUC, VALID_RUC)]


async def test_dni_lookup_and_status(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lookup_gateway: FakeLookup
) -> None:
    admin = authorization_for(restaurant_a.admin)

    found = await client.get(url(f"lookup/dni/{VALID_DNI}"), headers=admin)
    status = await client.get(url("lookup/status"), headers=admin)

    assert found.json()["document_type"] == "dni"
    assert status.json() == {"enabled": True}
    lookup_gateway.enabled = False
    assert (await client.get(url("lookup/status"), headers=admin)).json() == {"enabled": False}


async def test_invalid_numbers_are_422_without_calling_the_provider(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lookup_gateway: FakeLookup
) -> None:
    headers = authorization_for(restaurant_a.admin)

    bad_ruc = await client.get(url("lookup/ruc/20552103817"), headers=headers)
    bad_dni = await client.get(url("lookup/dni/1234"), headers=headers)

    assert bad_ruc.status_code == bad_dni.status_code == 422
    assert bad_ruc.json()["code"] == ErrorCode.INVALID_DOCUMENT_NUMBER.value
    assert not lookup_gateway.calls


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (DocumentNotFoundError(), 404, ErrorCode.DOCUMENT_NOT_FOUND),
        (LookupUnavailableError(), 503, ErrorCode.LOOKUP_UNAVAILABLE),
    ],
)
async def test_provider_errors_are_mapped(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    lookup_gateway: FakeLookup,
    error: Exception,
    status_code: int,
    code: ErrorCode,
) -> None:
    lookup_gateway.error = error

    response = await client.get(
        url(f"lookup/dni/{VALID_DNI}"), headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == status_code
    assert response.json()["code"] == code.value


async def test_disabled_lookup_is_503_with_its_own_code(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lookup_gateway: FakeLookup
) -> None:
    lookup_gateway.enabled = False

    response = await client.get(
        url(f"lookup/dni/{VALID_DNI}"), headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 503
    assert response.json()["code"] == ErrorCode.LOOKUP_DISABLED.value


async def test_each_account_has_its_own_limit(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lookup_gateway: FakeLookup
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    path = url(f"lookup/dni/{VALID_DNI}")

    codes = [(await client.get(path, headers=waiter)).status_code for _ in range(4)]
    blocked = await client.get(path, headers=waiter)
    admin = await client.get(path, headers=authorization_for(restaurant_a.admin))

    assert codes == [200, 200, 200, 429]
    assert blocked.headers["Retry-After"].isdigit()
    assert blocked.json()["code"] == ErrorCode.LOOKUP_RATE_LIMITED.value
    assert admin.status_code == 200


async def test_lookup_needs_a_permission_and_a_session(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    lookup_gateway: FakeLookup,
) -> None:
    path = url(f"lookup/dni/{VALID_DNI}")
    kitchen = await staff_with_role(session, restaurant_a, "Cocina", {Permission.MENU_READ.value})
    cashier = await staff_with_role(
        session, restaurant_a, "Clientes", {Permission.CUSTOMERS_MANAGE.value}
    )

    anonymous = await client.get(path)
    forbidden = await client.get(path, headers=authorization_for(kitchen))
    forbidden_status = await client.get(url("lookup/status"), headers=authorization_for(kitchen))
    allowed = await client.get(path, headers=authorization_for(cashier))

    assert anonymous.status_code == 401
    assert forbidden.status_code == forbidden_status.status_code == 403
    assert allowed.status_code == 200
    assert len(lookup_gateway.calls) == 1
