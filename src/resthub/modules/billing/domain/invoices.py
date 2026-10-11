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

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.modules.billing.domain.exceptions import (
    InvalidBillingSettingsError,
    InvalidInvoiceError,
)

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
# Espacios o caracteres de control en una URL son señal de algo armado a mano.
_UNSAFE_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")
_LOCAL_HOSTS = ("localhost", ".localhost", ".internal", ".local")


def provider_url_problem(raw: str) -> str | None:
    """Qué tiene de malo la URL del proveedor, o `None` si sirve (o si está vacía).

    El servidor le manda a esa URL cada comprobante con el token del local, así
    que no puede apuntar a cualquier lado: solo `https://` a un host con
    nombre, sin usuario ni contraseña en la URL y sin direcciones internas
    (`localhost`, redes privadas, la de metadatos de la nube). Vacía es un local
    sin proveedor todavía.
    """
    url = raw.strip()
    if not url:
        return None
    if _UNSAFE_URL_CHARS.search(url):
        return "La URL del proveedor no puede llevar espacios."
    try:
        parts = urlsplit(url)
        # `port` recién valida el puerto al leerlo.
        _ = parts.port
    except ValueError:
        return "La URL del proveedor no es válida."
    if parts.scheme.lower() != "https":
        return "La URL del proveedor tiene que empezar con https://."
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "La URL del proveedor no puede llevar usuario ni contraseña; el token va aparte."
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return "La URL del proveedor no tiene servidor."
    if host == "localhost" or host.endswith(_LOCAL_HOSTS):
        return "La URL del proveedor no puede apuntar a una dirección interna."
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_global:
        return "La URL del proveedor no puede apuntar a una dirección interna."
    return None


def validate_provider_url(raw: str) -> str:
    problem = provider_url_problem(raw)
    if problem is not None:
        raise InvalidBillingSettingsError(problem)
    return raw.strip()


def _destination(url: str) -> tuple[str, str, int | None, str, str]:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").rstrip(".").lower()
    return (parts.scheme.lower(), host, parts.port, parts.path, parts.query)


def same_provider_destination(before: str, after: str) -> bool:
    """Si dos URLs del proveedor llevan al mismo lugar.

    El token del local se cargó para una ruta: si la ruta cambia, el token no
    la sigue solo. Mayúsculas en el esquema o el servidor no cuentan como cambio.
    """
    try:
        return _destination(before) == _destination(after)
    except ValueError:
        return before.strip() == after.strip()


def provider_host(url: str) -> str:
    """El servidor de la URL del proveedor, para la bitácora (nunca la ruta: puede ser secreta)."""
    try:
        return (urlsplit(url.strip()).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def safe_pdf_url(raw: str) -> str:
    """El enlace al PDF que devolvió el proveedor, solo si es `https://` a un servidor.

    La pantalla lo abre tal cual: un `javascript:` o un `http://` no se guarda.
    """
    url = raw.strip()
    if not url or _UNSAFE_URL_CHARS.search(url):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not parts.hostname:
        return ""
    return url


class InvoiceKind(StrEnum):
    SALES_RECEIPT = "boleta"
    TAX_INVOICE = "factura"

    @property
    def label(self) -> str:
        return "Boleta de venta" if self is InvoiceKind.SALES_RECEIPT else "Factura"


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
    sales_receipt_series: str = "B001"
    tax_invoice_series: str = "F001"
    provider_url: str = ""
    # Nunca sale en una respuesta: solo se sabe si está cargado.
    provider_token: str = ""
    # La del restaurante, no se guarda acá: la fecha de emisión es la del día
    # del local, no la de UTC.
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        self.ruc = self.ruc.strip()
        if self.ruc and not _RUC.fullmatch(self.ruc):
            raise InvalidBillingSettingsError(
                "El RUC del local tiene 11 dígitos y empieza en 10 o 20."
            )
        self.legal_name = " ".join(self.legal_name.split())[:MAX_CUSTOMER_NAME_LENGTH]
        self.address = " ".join(self.address.split())[:MAX_ADDRESS_LENGTH]
        rate = Decimal(self.igv_rate)
        if rate < 0 or rate > 30 or rate != rate.quantize(CENT):
            raise InvalidBillingSettingsError("La tasa de IGV va de 0 a 30 % con dos decimales.")
        self.igv_rate = rate.quantize(CENT)
        self.sales_receipt_series = self._series(
            self.sales_receipt_series, InvoiceKind.SALES_RECEIPT
        )
        self.tax_invoice_series = self._series(self.tax_invoice_series, InvoiceKind.TAX_INVOICE)
        self.provider_url = self.provider_url.strip()

    @staticmethod
    def _series(raw: str, kind: InvoiceKind) -> str:
        series = raw.strip().upper()
        if not _SERIES[kind.value].fullmatch(series):
            letter = "B" if kind is InvoiceKind.SALES_RECEIPT else "F"
            raise InvalidBillingSettingsError(
                f"La serie de {kind.label.lower()} son 4 caracteres y empieza con {letter}."
            )
        return series

    @property
    def is_ready(self) -> bool:
        """Si se puede emitir de verdad: datos del local y credenciales."""
        return bool(self.ruc and self.legal_name and self.provider_url and self.provider_token)

    def series_for(self, kind: InvoiceKind) -> str:
        return (
            self.sales_receipt_series
            if kind is InvoiceKind.SALES_RECEIPT
            else self.tax_invoice_series
        )


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
    if kind is InvoiceKind.TAX_INVOICE:
        if doc is not DocumentType.RUC or not _RUC.fullmatch(number):
            raise InvalidInvoiceError("Una factura necesita el RUC del cliente (11 dígitos).")
        if not name:
            raise InvalidInvoiceError("Una factura necesita la razón social del cliente.")
    elif doc is DocumentType.NONE:
        if total > ANONYMOUS_LIMIT:
            raise InvalidInvoiceError(
                f"Una boleta de más de S/ {ANONYMOUS_LIMIT} necesita el documento del cliente."
            )
        number, name = "", name or "Clientes varios"
    else:
        patterns = {DocumentType.DNI: _DNI, DocumentType.CE: _CE, DocumentType.RUC: _RUC}
        if not patterns[doc].fullmatch(number):
            raise InvalidInvoiceError(f"El número de {doc.value.upper()} no es válido.")
        if not name:
            raise InvalidInvoiceError("Escribe el nombre del cliente.")
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
        self.pdf_url = safe_pdf_url(pdf_url)[:500]
        self.provider_response = raw

    def ensure_resendable(self) -> None:
        if self.status is InvoiceStatus.ACCEPTED:
            raise InvalidInvoiceError(f"El comprobante {self.code} ya fue aceptado por SUNAT.")


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
