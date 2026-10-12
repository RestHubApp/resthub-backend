"""Cableado del adaptador HTTP de la consulta de documentos."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from resthub.core.config import get_settings
from resthub.modules.lookup.adapters.factiliza.client import FactilizaDocumentLookup
from resthub.modules.lookup.adapters.memory.cache import CachedDocumentLookup
from resthub.modules.lookup.adapters.memory.quota import PerAccountLookupQuota
from resthub.modules.lookup.ports.document_lookup import DocumentLookupGateway, LookupQuotaPolicy


@lru_cache
def get_document_lookup() -> DocumentLookupGateway:
    """Un solo proveedor con su memoria para todo el proceso; las pruebas lo reemplazan."""
    settings = get_settings()
    return CachedDocumentLookup(
        FactilizaDocumentLookup(
            token=settings.factiliza_token,
            base_url=settings.factiliza_base_url,
            timeout_seconds=settings.factiliza_timeout_seconds,
        ),
        ttl_seconds=settings.lookup_cache_seconds,
    )


@lru_cache
def get_lookup_quota() -> LookupQuotaPolicy:
    return PerAccountLookupQuota(get_settings().lookup_per_minute)


DocumentLookupDep = Annotated[DocumentLookupGateway, Depends(get_document_lookup)]
LookupQuotaDep = Annotated[LookupQuotaPolicy, Depends(get_lookup_quota)]
