"""Adaptador de entrada HTTP de la consulta de RUC y DNI.

Lo usa quien puede emitir comprobantes (`billing.issue`) o gestionar clientes
(`customers.manage`): autocompletar el alta de un cliente o los datos de una factura.
Consulta a un tercero (Factiliza) y no guarda nada.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from resthub.core.auth import require_permission
from resthub.core.errors import api_error
from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.modules.lookup.adapters.api.dependencies import DocumentLookupDep, LookupQuotaDep
from resthub.modules.lookup.adapters.api.schemas import (
    DocumentLookupResponse,
    LookupStatusResponse,
)
from resthub.modules.lookup.domain.documents import DocumentType
from resthub.modules.lookup.domain.exceptions import (
    DocumentLookupError,
    DocumentNotFoundError,
    InvalidDocumentNumberError,
    LookupDisabledError,
    LookupRateLimitedError,
    LookupUnavailableError,
)
from resthub.modules.lookup.use_cases.lookup_document import LookupDocument

router = APIRouter()

LookerDep = Annotated[
    Principal,
    Depends(require_permission(Permission.BILLING_ISSUE, Permission.CUSTOMERS_MANAGE)),
]
# Más largo que un RUC a propósito: el formato lo juzga el caso de uso y responde con su código.
NumberPath = Annotated[str, Path(max_length=20)]


def _http_error(error: DocumentLookupError) -> HTTPException:
    # Apagada responde 404 y no 503: no es una falla del servidor, y así el fuzzing (que corre
    # sin token) no la toma por un error 5xx.
    if isinstance(error, DocumentNotFoundError | LookupDisabledError):
        return api_error(status.HTTP_404_NOT_FOUND, error)
    if isinstance(error, InvalidDocumentNumberError):
        return api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)
    if isinstance(error, LookupRateLimitedError):
        return api_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            error,
            headers={"Retry-After": str(error.retry_after_seconds)},
        )
    if isinstance(error, LookupUnavailableError):
        return api_error(status.HTTP_503_SERVICE_UNAVAILABLE, error)
    return api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)


async def _lookup(
    principal: Principal,
    gateway: DocumentLookupDep,
    quota: LookupQuotaDep,
    document_type: DocumentType,
    number: str,
) -> DocumentLookupResponse:
    try:
        record = await LookupDocument(gateway, quota)(principal.user_id, document_type, number)
    except DocumentLookupError as error:
        raise _http_error(error) from error
    return DocumentLookupResponse.from_record(record)


@router.get("/status", response_model=LookupStatusResponse, summary="¿Está activa la búsqueda?")
async def read_status(_principal: LookerDep, gateway: DocumentLookupDep) -> LookupStatusResponse:
    return LookupStatusResponse(enabled=gateway.enabled)


@router.get("/ruc/{ruc}", response_model=DocumentLookupResponse, summary="Buscar un RUC en SUNAT")
async def lookup_ruc(
    ruc: NumberPath, principal: LookerDep, gateway: DocumentLookupDep, quota: LookupQuotaDep
) -> DocumentLookupResponse:
    return await _lookup(principal, gateway, quota, DocumentType.RUC, ruc)


@router.get("/dni/{dni}", response_model=DocumentLookupResponse, summary="Buscar un DNI en RENIEC")
async def lookup_dni(
    dni: NumberPath, principal: LookerDep, gateway: DocumentLookupDep, quota: LookupQuotaDep
) -> DocumentLookupResponse:
    return await _lookup(principal, gateway, quota, DocumentType.DNI, dni)
