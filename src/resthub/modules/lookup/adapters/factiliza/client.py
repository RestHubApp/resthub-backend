"""Consulta de RUC y DNI en Factiliza (https://docs.factiliza.com).

Factiliza es un tercero que consulta SUNAT y RENIEC; se declara en la política de
privacidad como encargado de tratamiento. Contrato (docs.factiliza.com/api-consulta):

- `GET {base}/ruc/info/{ruc}` y `GET {base}/dni/info/{dni}`, con
  `Authorization: Bearer <token>`.
- 200: `{"status", "success", "message", "data": {...}}`. En RUC, `data` trae
  `nombre_o_razon_social`, `estado`, `condicion`, `direccion_completa`; en DNI,
  `nombres`, `apellido_paterno`, `apellido_materno`, `nombre_completo`, `direccion_completa`.
- 400: `{"status": 400, "success": false, "message": "Bad Request"}`. La documentación no
  distingue «número inexistente» de otros 400; como el formato se valida antes, un 400 se
  toma por «no se encontró».

Una sola llamada, sin reintentos y con un plazo corto: el plan gratuito tiene 100
consultas al mes. Ni el token ni el número consultado se escriben en los logs.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx

from resthub.core.logs import get_logger
from resthub.modules.lookup.domain.documents import DocumentRecord, DocumentType
from resthub.modules.lookup.domain.exceptions import (
    DocumentNotFoundError,
    LookupUnavailableError,
)

logger = get_logger("resthub.lookup.factiliza")

_PATHS = {DocumentType.RUC: "ruc", DocumentType.DNI: "dni"}
_NOT_FOUND_STATUSES = frozenset({HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND})


def _text(value: object) -> str | None:
    clean = str(value).strip() if value is not None else ""
    return clean or None


def _person_name(data: dict[str, Any]) -> str | None:
    parts = [data.get("nombres"), data.get("apellido_paterno"), data.get("apellido_materno")]
    joined = " ".join(part for part in (_text(item) for item in parts) if part)
    return joined or _text(data.get("nombre_completo"))


def parse_record(document_type: DocumentType, number: str, body: object) -> DocumentRecord:
    """El `data` de una respuesta 200 como registro; `DocumentNotFoundError` si viene vacío."""
    if not isinstance(body, dict) or body.get("success") is False:
        raise DocumentNotFoundError
    data = body.get("data")
    if not isinstance(data, dict):
        raise DocumentNotFoundError
    if document_type is DocumentType.RUC:
        name = _text(data.get("nombre_o_razon_social"))
        status = _text(data.get("estado"))
        condition = _text(data.get("condicion"))
    else:
        name = _person_name(data)
        status = condition = None
    if name is None:
        raise DocumentNotFoundError
    return DocumentRecord(
        document_type=document_type,
        document_number=number,
        name=name,
        address=_text(data.get("direccion_completa")) or _text(data.get("direccion")),
        status=status,
        condition=condition,
    )


class FactilizaDocumentLookup:
    def __init__(
        self,
        token: str,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        # El transporte se inyecta en las pruebas: ninguna llama a Factiliza de verdad.
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    async def lookup(self, document_type: DocumentType, number: str) -> DocumentRecord:
        endpoint = f"{self._base_url}/{_PATHS[document_type]}/info/{number}"
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as http:
                response = await http.get(
                    endpoint, headers={"Authorization": f"Bearer {self._token}"}
                )
        except httpx.HTTPError as error:
            logger.warning(
                "lookup.unreachable", kind=document_type.value, error=type(error).__name__
            )
            raise LookupUnavailableError from error
        if response.status_code in _NOT_FOUND_STATUSES:
            raise DocumentNotFoundError
        if response.status_code != HTTPStatus.OK:
            # 401/403 (token inválido), cuota del plan agotada u otro fallo del proveedor.
            logger.warning(
                "lookup.provider_error", kind=document_type.value, status=response.status_code
            )
            raise LookupUnavailableError
        try:
            body = response.json()
        except ValueError as error:
            logger.warning("lookup.invalid_body", kind=document_type.value)
            raise LookupUnavailableError from error
        record = parse_record(document_type, number, body)
        logger.info("lookup.done", kind=document_type.value)
        return record
