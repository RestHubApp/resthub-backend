"""Esquemas HTTP de la consulta de documentos."""

from __future__ import annotations

from pydantic import BaseModel

from resthub.modules.lookup.domain.documents import DocumentRecord, DocumentType


class LookupStatusResponse(BaseModel):
    """Si la búsqueda por RUC/DNI está activa; si no, el frontend no ofrece el botón."""

    enabled: bool


class DocumentLookupResponse(BaseModel):
    """Lo que dice SUNAT (RUC) o RENIEC (DNI). Se rellena el formulario; no se guarda."""

    document_type: DocumentType
    document_number: str
    # Razón social (RUC) o nombres y apellidos (DNI).
    name: str
    address: str | None
    # Solo RUC: ACTIVO, SUSPENSION TEMPORAL, BAJA DE OFICIO…
    status: str | None
    # Solo RUC: HABIDO, NO HABIDO…
    condition: str | None

    @classmethod
    def from_record(cls, record: DocumentRecord) -> DocumentLookupResponse:
        return cls(
            document_type=record.document_type,
            document_number=record.document_number,
            name=record.name,
            address=record.address,
            status=record.status,
            condition=record.condition,
        )
