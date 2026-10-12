"""Puertos de la consulta de documentos."""

from __future__ import annotations

from typing import Protocol

from resthub.modules.lookup.domain.documents import DocumentRecord, DocumentType


class DocumentLookupGateway(Protocol):
    """Registro oficial de RUC (SUNAT) y DNI (RENIEC) detrás de un tercero."""

    @property
    def enabled(self) -> bool:
        """Falso si falta la credencial del tercero: no se ofrece la función."""
        ...

    async def lookup(self, document_type: DocumentType, number: str) -> DocumentRecord:
        """El registro del documento.

        Lanza `DocumentNotFoundError` si no existe y `LookupUnavailableError` si el
        tercero falla, tarda o rechaza la credencial.
        """
        ...


class LookupQuotaPolicy(Protocol):
    def acquire(self, user_id: int) -> int:
        """Anota una consulta de la cuenta. 0 si pasa; si no, los segundos a esperar."""
        ...
