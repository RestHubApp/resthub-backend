"""Puertos del módulo de comprobantes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
        """Guarda un comprobante nuevo; `InvoiceNumberTaken` si otro ganó el número o el pedido."""
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


class PaidOrderDirectory(Protocol):
    async def get(self, restaurant_id: int, order_id: int) -> PaidOrder | None:
        """El pedido si existe en ese local y está pagado."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderResult:
    status: InvoiceStatus
    message: str
    pdf_url: str = ""
    raw: dict[str, Any] | None = None


class ElectronicInvoicer(Protocol):
    """Quien envía el comprobante a SUNAT: un proveedor autorizado (OSE/PSE)."""

    async def send(
        self, settings: BillingSettings, invoice: Invoice, kind: InvoiceKind
    ) -> ProviderResult: ...
