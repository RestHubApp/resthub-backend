"""Dobles del correo y del respaldo de comprobantes: ninguna prueba llama a Brevo ni a Drive."""

from __future__ import annotations

from collections.abc import Sequence

from resthub.modules.billing.domain.exceptions import DocumentArchiveError, EmailDeliveryError
from resthub.modules.billing.ports.delivery_ports import FetchedDocument, OutgoingEmail


class FakeEmailGateway:
    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self.enabled = enabled
        self.fail = fail
        self.sent: list[OutgoingEmail] = []

    def is_enabled(self) -> bool:
        return self.enabled

    async def send(self, email: OutgoingEmail) -> None:
        if self.fail:
            raise EmailDeliveryError
        self.sent.append(email)


class FakeFetcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedDocument:
        self.urls.append(url)
        if self.fail:
            message = "No se pudo descargar el documento del proveedor."
            raise DocumentArchiveError(message)
        return FetchedDocument(content=b"contenido", media_type="application/octet-stream")


class FakeArchive:
    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self.enabled = enabled
        self.fail = fail
        self.stored: list[tuple[tuple[str, ...], str]] = []

    def is_enabled(self) -> bool:
        return self.enabled

    async def store(
        self,
        folder: Sequence[str],
        filename: str,
        document: FetchedDocument,
    ) -> str:
        if self.fail:
            message = "Google Drive rechazó la subida (HTTP 403)."
            raise DocumentArchiveError(message)
        self.stored.append((tuple(folder), filename))
        return f"https://drive.example/{filename}"
