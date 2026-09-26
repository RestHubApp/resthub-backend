"""Comprobantes de pago electrónicos (SUNAT): boletas y facturas.

Python puro. En el local los precios de la carta ya incluyen el IGV; acá se
separa la base imponible del impuesto con la tasa que configuró el
restaurante. Un pedido pagado tiene a lo sumo un comprobante vigente.

Reglas de SUNAT que se aplican antes de emitir:

- Una factura exige el RUC del cliente (11 dígitos) y su razón social.
- Una boleta puede ir sin documento («clientes varios»), salvo que el total
  pase de S/ 700: entonces exige DNI, carné de extranjería o RUC.
- Cada serie lleva su correlativo: B001-1, B001-2… y F001-1, F001-2…
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from resthub.modules.billing.domain.exceptions import InvalidBillingSettings, InvalidInvoice

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal(100)
# El monto a partir del cual una boleta exige identificar al cliente.
ANONYMOUS_LIMIT = Decimal("700.00")
DEFAULT_IGV_RATE = Decimal("18.00")
MAX_CUSTOMER_NAME_LENGTH = 100
MAX_ADDRESS_LENGTH = 200
_SERIES = {"boleta": re.compile(r"B[A-Z0-9]{3}"), "factura": re.compile(r"F[A-Z0-9]{3}")}
_RUC = re.compile(r"(10|15|17|20)\d{9}")
_DNI = re.compile(r"\d{8}")
_CE = re.compile(r"[A-Z0-9]{8,12}")


class InvoiceKind(StrEnum):
    BOLETA = "boleta"
    FACTURA = "factura"

    @property
    def label(self) -> str:
        return "Boleta de venta" if self is InvoiceKind.BOLETA else "Factura"


class DocumentType(StrEnum):
    """Tipo de documento del cliente, con el código del catálogo 6 de SUNAT."""

    NONE = "none"
    DNI = "dni"
    CE = "ce"
    RUC = "ruc"

    @property
    def sunat_code(self) -> str:
        return {"none": "-", "dni": "1", "ce": "4", "ruc": "6"}[self.value]


class InvoiceStatus(StrEnum):
    # SUNAT lo aceptó (a través del proveedor).
    ACCEPTED = "accepted"
    # El proveedor lo recibió y SUNAT todavía no responde, o falló la conexión.
    PENDING = "pending"
    # SUNAT o el proveedor lo rechazaron: hay que corregir y reenviar.
    REJECTED = "rejected"
    # No hay proveedor configurado: el comprobante existe en RestHub pero no
    # se envió a SUNAT. Se reenvía cuando el local cargue sus credenciales.
    SIMULATED = "simulated"

    @property
    def label(self) -> str:
        return {
            "accepted": "Aceptado por SUNAT",
            "pending": "Pendiente de envío",
            "rejected": "Rechazado",
            "simulated": "Sin enviar (sin proveedor)",
        }[self.value]


@dataclass(slots=True)
class BillingSettings:
    """Los datos fiscales del local y cómo se conecta con el proveedor."""

    restaurant_id: int
    ruc: str = ""
    legal_name: str = ""
    address: str = ""
    # En porcentaje. 18 es el régimen general; un restaurante MYPE acogido a
    # la Ley 31556 aplica la tasa reducida vigente.
    igv_rate: Decimal = DEFAULT_IGV_RATE
    boleta_series: str = "B001"
    factura_series: str = "F001"
    provider_url: str = ""
    # Nunca sale en una respuesta: solo se sabe si está cargado.
    provider_token: str = ""

    def __post_init__(self) -> None:
        self.ruc = self.ruc.strip()
        if self.ruc and not _RUC.fullmatch(self.ruc):
            raise InvalidBillingSettings("El RUC del local tiene 11 dígitos y empieza en 10 o 20.")
        self.legal_name = " ".join(self.legal_name.split())[:MAX_CUSTOMER_NAME_LENGTH]
        self.address = " ".join(self.address.split())[:MAX_ADDRESS_LENGTH]
        rate = Decimal(self.igv_rate)
        if rate < 0 or rate > 30 or rate != rate.quantize(CENT):
            raise InvalidBillingSettings("La tasa de IGV va de 0 a 30 % con dos decimales.")
        self.igv_rate = rate.quantize(CENT)
        self.boleta_series = self._series(self.boleta_series, InvoiceKind.BOLETA)
        self.factura_series = self._series(self.factura_series, InvoiceKind.FACTURA)
        self.provider_url = self.provider_url.strip()

    @staticmethod
    def _series(raw: str, kind: InvoiceKind) -> str:
        series = raw.strip().upper()
        if not _SERIES[kind.value].fullmatch(series):
            letra = "B" if kind is InvoiceKind.BOLETA else "F"
            raise InvalidBillingSettings(
                f"La serie de {kind.label.lower()} son 4 caracteres y empieza con {letra}."
            )
        return series

    @property
    def is_ready(self) -> bool:
        """Si se puede emitir de verdad: datos del local y credenciales."""
        return bool(self.ruc and self.legal_name and self.provider_url and self.provider_token)

    def series_for(self, kind: InvoiceKind) -> str:
        return self.boleta_series if kind is InvoiceKind.BOLETA else self.factura_series


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    description: str
    quantity: int
    # Precio unitario con IGV, como en la carta.
    unit_price: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class Customer:
    document_type: DocumentType = DocumentType.NONE
    document_number: str = ""
    name: str = ""
    address: str = ""


def split_igv(total: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """La base imponible y el IGV de un monto que ya incluye el impuesto."""
    base = (total * HUNDRED / (HUNDRED + rate)).quantize(CENT, rounding=ROUND_HALF_UP)
    return base, (total - base).quantize(CENT)


def validate_customer(kind: InvoiceKind, customer: Customer, total: Decimal) -> Customer:
    number = customer.document_number.strip().upper()
    name = " ".join(customer.name.split())[:MAX_CUSTOMER_NAME_LENGTH]
    address = " ".join(customer.address.split())[:MAX_ADDRESS_LENGTH]
    doc = customer.document_type
    if kind is InvoiceKind.FACTURA:
        if doc is not DocumentType.RUC or not _RUC.fullmatch(number):
            raise InvalidInvoice("Una factura necesita el RUC del cliente (11 dígitos).")
        if not name:
            raise InvalidInvoice("Una factura necesita la razón social del cliente.")
    elif doc is DocumentType.NONE:
        if total > ANONYMOUS_LIMIT:
            raise InvalidInvoice(
                f"Una boleta de más de S/ {ANONYMOUS_LIMIT} necesita el documento del cliente."
            )
        number, name = "", name or "Clientes varios"
    else:
        patterns = {DocumentType.DNI: _DNI, DocumentType.CE: _CE, DocumentType.RUC: _RUC}
        if not patterns[doc].fullmatch(number):
            raise InvalidInvoice(f"El número de {doc.value.upper()} no es válido.")
        if not name:
            raise InvalidInvoice("Escribe el nombre del cliente.")
    return Customer(document_type=doc, document_number=number, name=name, address=address)


@dataclass(slots=True)
class Invoice:
    restaurant_id: int
    order_id: int
    kind: InvoiceKind
    series: str
    number: int
    customer: Customer
    lines: tuple[InvoiceLine, ...]
    # Lo que se cobró: con descuentos y sin propinas.
    total: Decimal
    discount: Decimal
    igv_rate: Decimal
    issued_by: int
    status: InvoiceStatus = InvoiceStatus.PENDING
    pdf_url: str = ""
    provider_message: str = ""
    provider_response: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    issued_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def taxable(self) -> Decimal:
        return split_igv(self.total, self.igv_rate)[0]

    @property
    def igv(self) -> Decimal:
        return split_igv(self.total, self.igv_rate)[1]

    @property
    def code(self) -> str:
        """«B001-15»: como se lee en el comprobante impreso."""
        return f"{self.series}-{self.number}"

    def record_result(
        self, status: InvoiceStatus, message: str, pdf_url: str, raw: dict[str, Any]
    ) -> None:
        self.status = status
        self.provider_message = message[:300]
        self.pdf_url = pdf_url[:500]
        self.provider_response = raw

    def ensure_resendable(self) -> None:
        if self.status is InvoiceStatus.ACCEPTED:
            raise InvalidInvoice(f"El comprobante {self.code} ya fue aceptado por SUNAT.")


def build_lines(items: Sequence[tuple[str, int, Decimal, bool]]) -> tuple[InvoiceLine, ...]:
    """Las líneas del comprobante desde los ítems del pedido.

    Las cortesías no se cobran, así que no van como venta: SUNAT las trata
    como transferencias gratuitas, que este comprobante no emite.
    """
    return tuple(
        InvoiceLine(
            description=name[:250],
            quantity=quantity,
            unit_price=unit_price.quantize(CENT),
            total=(unit_price * quantity).quantize(CENT),
        )
        for name, quantity, unit_price, is_courtesy in items
        if not is_courtesy
    )
