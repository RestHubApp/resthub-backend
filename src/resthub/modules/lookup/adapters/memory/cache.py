"""Memoria breve de las consultas resueltas.

Ahorra cuota del plan (varios locales piden el mismo RUC) y tiempo. Solo guarda
consultas que salieron bien, solo en la memoria del proceso y solo unos minutos.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from resthub.modules.lookup.domain.documents import DocumentRecord, DocumentType
from resthub.modules.lookup.ports.document_lookup import DocumentLookupGateway

MAX_ENTRIES = 500


class CachedDocumentLookup:
    def __init__(
        self,
        inner: DocumentLookupGateway,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[tuple[DocumentType, str], tuple[float, DocumentRecord]] = {}

    @property
    def enabled(self) -> bool:
        return self._inner.enabled

    async def lookup(self, document_type: DocumentType, number: str) -> DocumentRecord:
        key = (document_type, number)
        now = self._clock()
        cached = self._entries.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        record = await self._inner.lookup(document_type, number)
        if self._ttl > 0:
            if len(self._entries) >= MAX_ENTRIES:
                self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
            if len(self._entries) < MAX_ENTRIES:
                self._entries[key] = (now + self._ttl, record)
        return record
