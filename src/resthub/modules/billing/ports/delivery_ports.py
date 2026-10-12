"""Puertos de salida de los comprobantes: correo al cliente y respaldo de sus documentos."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to_address: str
    to_name: str
    subject: str
    html: str
    text: str


class EmailGateway(Protocol):
    """Quien entrega un correo: un servicio transaccional por HTTPS (Brevo)."""

    def is_enabled(self) -> bool:
        """Falso sin credenciales: la función queda apagada y la pantalla no la ofrece."""
        ...

    async def send(self, email: OutgoingEmail) -> None:
        """Entrega el correo; `EmailDeliveryError` si no se pudo. Nunca registra la dirección."""
        ...


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    content: bytes
    media_type: str


class DocumentFetchGateway(Protocol):
    """Descarga un documento (PDF, XML, CDR) del enlace que dio el proveedor de comprobantes."""

    async def fetch(self, url: str) -> FetchedDocument:
        """`DocumentArchiveError` si el enlace no sirve, no responde o pesa demasiado."""
        ...


class DocumentArchiveGateway(Protocol):
    """Guarda documentos en un almacenamiento privado propio (Google Drive)."""

    def is_enabled(self) -> bool:
        """Falso sin credenciales: el respaldo queda apagado."""
        ...

    async def store(self, folder: Sequence[str], filename: str, document: FetchedDocument) -> str:
        """Sube el documento a la carpeta `folder` (se crea si falta) y devuelve su enlace privado.

        Es idempotente por nombre: si ya existe uno igual en esa carpeta no lo duplica.
        `DocumentArchiveError` si falla.
        """
        ...


class InvoiceArchiveHook(Protocol):
    """Aviso de que un comprobante tiene documentos por respaldar.

    Quien lo implementa lanza el respaldo fuera de la petición, cuando la
    transacción que guardó el comprobante se confirma.
    """

    def is_enabled(self) -> bool: ...

    def schedule(self, restaurant_id: int, invoice_id: int) -> None: ...
