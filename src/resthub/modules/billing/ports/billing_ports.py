"""Puertos del módulo de comprobantes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from resthub.core.pagination import Page
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
)


class BillingSettingsRepository(Protocol):
    async def get(self, restaurant_id: int) -> BillingSettings:
        """Los datos fiscales del local y su zona horaria; sin cargar, los valores por omisión."""
        ...

    async def save(self, settings: BillingSettings) -> BillingSettings: ...


@dataclass(frozen=True, slots=True)
class InvoiceQuery:
    restaurant_id: int
    date_from: date | None = None
    date_to: date | None = None
    status: InvoiceStatus | None = None
    limit: int = 25
    offset: int = 0


class InvoiceRepository(Protocol):
    async def add(self, invoice: Invoice) -> Invoice:
        """Guarda un comprobante nuevo; `InvoiceNumberTakenError` si otro ganó el número."""
        ...

    async def get(
        self, restaurant_id: int, invoice_id: int, *, for_update: bool = False
    ) -> Invoice | None:
        """Con `for_update`, el comprobante queda tomado hasta el fin de la transacción."""
        ...

    async def for_order(self, restaurant_id: int, order_id: int) -> Invoice | None: ...

    async def save(self, invoice: Invoice) -> Invoice: ...

    async def search(self, query: InvoiceQuery) -> Page[Invoice]: ...

    async def next_number(self, restaurant_id: int, series: str) -> int:
        """El siguiente correlativo de la serie; toma el turno hasta el fin de la transacción."""
        ...


@dataclass(frozen=True, slots=True)
class PaidOrder:
    """Lo que un comprobante necesita de un pedido pagado, leído de `orders`."""

    id: int
    number: int
    total: Decimal
    discount: Decimal
    # (nombre con opciones, cantidad, precio unitario con IGV, es cortesía)
    items: tuple[tuple[str, int, Decimal, bool], ...]
    # Quien tomó el pedido: sin `orders.read_all`, solo él lo ve ya cobrado.
    waiter_id: int


class PaidOrderDirectory(Protocol):
    async def get(self, restaurant_id: int, order_id: int) -> PaidOrder | None:
        """El pedido si existe en ese local y está pagado."""
        ...

    async def waiter_of(self, restaurant_id: int, order_id: int) -> int | None:
        """Quién tomó el pedido, si existe en ese local (en cualquier estado)."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderResult:
    status: InvoiceStatus
    message: str
    raw: dict[str, Any] | None = None
    # El XML firmado, la constancia de SUNAT (CDR: un ZIP en base64) y el documento
    # que se envió, para volver a armar el PDF. Vacíos si no llegó a enviarse.
    signed_xml: str = ""
    cdr_zip: str = ""
    document_payload: dict[str, Any] = field(default_factory=dict)


class ElectronicInvoiceGateway(Protocol):
    """El servicio que firma los comprobantes y los envía a SUNAT (Lycet, de Greenter)."""

    async def send(
        self, settings: BillingSettings, invoice: Invoice, kind: InvoiceKind
    ) -> ProviderResult:
        """Nunca levanta por un fallo del servicio: lo devuelve como estado del comprobante."""
        ...

    async def render_pdf(self, invoice: Invoice) -> bytes:
        """El PDF del comprobante; `BillingProviderError` si el servicio no lo entrega."""
        ...

    async def register(self, settings: BillingSettings) -> None:
        """Deja al local listo para firmar: su RUC, usuario SOL y certificado.

        `BillingProviderError` si el servicio no responde o no lo acepta.
        """
        ...

    async def unregister(self, ruc: str) -> None:
        """Olvida un RUC que el local ya no usa. Nunca levanta: es limpieza."""
        ...


@dataclass(frozen=True, slots=True)
class ParsedCertificate:
    # La llave privada y el certificado en PEM, como los pide el servicio de facturación.
    pem: str
    expires_at: datetime | None
    # Quién figura en el certificado («TI SOLUCIONES»), para la bitácora.
    subject: str


class CertificateReader(Protocol):
    def read(self, content: bytes, password: str) -> ParsedCertificate:
        """Abre un .pfx/.p12 (con su contraseña) o un .pem; `InvalidCertificateError` si no."""
        ...


class SandboxDirectory(Protocol):
    """Si un local es el de muestra de la vista previa, leído de `restaurants`.

    El local de muestra emite comprobantes como cualquiera, pero nunca los
    manda a SUNAT: sus pedidos son de mentira.
    """

    async def is_sandbox(self, restaurant_id: int) -> bool: ...
