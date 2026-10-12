"""Descarga del PDF, el XML y el CDR desde los enlaces que devuelve el proveedor.

Los enlaces los dio el proveedor en su respuesta (Nubefact: `enlace_del_pdf`,
`enlace_del_xml`, `enlace_del_cdr`). Solo `https://` a un servidor con nombre o
dirección pública, sin seguir redirecciones y con un tope de tamaño: el enlace
sale de una respuesta de terceros y no puede llevar al servidor a leer su red.
"""

from __future__ import annotations

import httpx

from resthub.core.logs import get_logger
from resthub.modules.billing.domain.exceptions import DocumentArchiveError
from resthub.modules.billing.domain.invoices import provider_url_problem
from resthub.modules.billing.ports.delivery_ports import FetchedDocument

logger = get_logger("resthub.billing.documents")

TIMEOUT_SECONDS = 20.0
# Un ticket PDF o un XML pesan decenas de KB; el tope también deja subirlos de una vez.
MAX_BYTES = 5 * 1024 * 1024
_CHUNK = 64 * 1024


class ProviderDocumentFetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(self, url: str) -> FetchedDocument:
        if not url.lower().startswith("https://") or provider_url_problem(url) is not None:
            message = "El enlace del documento no es seguro; no se descargó."
            raise DocumentArchiveError(message)
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport, timeout=TIMEOUT_SECONDS, follow_redirects=False
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code != httpx.codes.OK:
                    logger.warning("billing.document_download_failed", status=response.status_code)
                    message = f"El proveedor no entregó el documento (HTTP {response.status_code})."
                    raise DocumentArchiveError(message)
                content = bytearray()
                async for chunk in response.aiter_bytes(_CHUNK):
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        message = "El documento del proveedor pesa demasiado; no se descargó."
                        raise DocumentArchiveError(message)
                media_type = response.headers.get("content-type", "application/octet-stream")
        except httpx.HTTPError as error:
            logger.warning("billing.document_unreachable", error=type(error).__name__)
            message = "No se pudo descargar el documento del proveedor."
            raise DocumentArchiveError(message) from error
        return FetchedDocument(content=bytes(content), media_type=media_type.split(";")[0].strip())
