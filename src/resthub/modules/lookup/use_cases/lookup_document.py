"""Consultar un RUC o DNI para autocompletar un formulario.

No guarda nada: el resultado vuelve a quien preguntó y solo se usa si lo confirma.
"""

from __future__ import annotations

from resthub.modules.lookup.domain.documents import (
    DocumentRecord,
    DocumentType,
    ensure_valid_document,
)
from resthub.modules.lookup.domain.exceptions import LookupDisabledError, LookupRateLimitedError
from resthub.modules.lookup.ports.document_lookup import DocumentLookupGateway, LookupQuotaPolicy


class LookupDocument:
    def __init__(self, gateway: DocumentLookupGateway, quota: LookupQuotaPolicy) -> None:
        self._gateway = gateway
        self._quota = quota

    async def __call__(
        self, user_id: int, document_type: DocumentType, number: str
    ) -> DocumentRecord:
        if not self._gateway.enabled:
            raise LookupDisabledError
        clean = ensure_valid_document(document_type, number)
        wait = self._quota.acquire(user_id)
        if wait > 0:
            raise LookupRateLimitedError(wait)
        return await self._gateway.lookup(document_type, clean)
