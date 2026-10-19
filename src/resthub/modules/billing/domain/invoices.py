"""Comprobantes de pago electrónicos (SUNAT): boletas y facturas.

Python puro. En el local los precios de la carta ya incluyen el IGV; acá se
separa la base imponible del impuesto con la tasa que configuró el
restaurante. Un pedido pagado tiene a lo sumo un comprobante vigente: uno
anulado (ver `credit_notes.py`) deja emitir otro para el mismo pedido.

Reglas de SUNAT que se aplican antes de emitir:

- Una factura exige el RUC del cliente (11 dígitos) y su razón social.
- Una boleta puede ir sin documento («clientes varios»), salvo que el total
  pase de S/ 700: entonces exige DNI, carné de extranjería o RUC.
- Cada serie lleva su correlativo: B001-1, B001-2… y F001-1, F001-2…
- Las notas de crédito tienen series propias con la letra del comprobante que
  afectan: BC01 para las de boletas y FC01 para las de facturas.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from resthub.core.clock import utc_now
from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.modules.billing.domain.exceptions import (
    InvalidBillingSettingsError,
    InvalidInvoiceError,
    InvoiceAlreadyVoidedError,
    InvoiceNotVoidableError,
)

if TYPE_CHECKING:
    from resthub.modules.billing.domain.credit_notes import CreditNote

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
# El usuario SOL secundario que da SUNAT, sin el RUC delante: letras y números.
_SOL_USER = re.compile(r"[A-Z0-9]{3,20}")
MAX_SOL_PASSWORD_LENGTH = 100
MAX_IGV_PERCENT = Decimal(30)
# El motivo de una anulación: va a SUNAT en la nota de crédito («desMotivo»).
MIN_VOID_REASON_LENGTH = 3
MAX_VOID_REASON_LENGTH = 200


class InvoiceKind(StrEnum):
    SALES_RECEIPT = "boleta"
    TAX_INVOICE = "factura"

    @property
    def label(self) -> str:
        return "Boleta de venta" if self is InvoiceKind.SALES_RECEIPT else "Factura"


class TaxRegime(StrEnum):
    """El régimen tributario del local: decide qué comprobantes puede emitir.

    El Nuevo RUS solo emite boletas de venta; los demás regímenes emiten boletas
    y facturas. La tasa de IGV es un dato aparte (`igv_rate`).
    """

    GENERAL = "general"
    SPECIAL = "special"
    MYPE = "mype"
    NEW_RUS = "new_rus"

    @property
    def label(self) -> str:
        return {
            "general": "Régimen general",
            "special": "Régimen especial (RER)",
            "mype": "Régimen MYPE tributario (RMT)",
            "new_rus": "Nuevo RUS",
        }[self.value]

    @property
    def allows_tax_invoice(self) -> bool:
        return self is not TaxRegime.NEW_RUS


class ArchiveStatus(StrEnum):
    """Estado del respaldo del XML y el PDF de un comprobante en el almacenamiento propio."""

    # No aplica: el respaldo está apagado o el comprobante aún no tiene documentos que guardar.
    DISABLED = "disabled"
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {
            "disabled": "Sin respaldo",
            "pending": "Respaldo en curso",
            "done": "Respaldado",
            "failed": "El respaldo falló",
        }[self.value]


def mask_email(address: str) -> str:
    """«a•••@correo.pe»: para la bitácora, que no guarda el correo completo."""
    local, _, domain = address.strip().partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"


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
    """Los datos fiscales del local y sus credenciales de SUNAT."""

    restaurant_id: int
    ruc: str = ""
    legal_name: str = ""
    address: str = ""
    # En porcentaje. 18 es el régimen general; un restaurante MYPE acogido a
    # la Ley 31556 aplica la tasa reducida vigente.
    igv_rate: Decimal = DEFAULT_IGV_RATE
    # Qué comprobantes puede emitir el local. Los locales anteriores quedan en el general.
    tax_regime: TaxRegime = TaxRegime.GENERAL
    sales_receipt_series: str = "B001"
    tax_invoice_series: str = "F001"
    # Las series de las notas de crédito: empiezan con la letra del comprobante que anulan.
    sales_receipt_credit_series: str = "BC01"
    tax_invoice_credit_series: str = "FC01"
    # Credenciales de SUNAT del local, cifradas en reposo. Nunca salen en una
    # respuesta ni en los logs: solo se sabe si están cargadas.
    # Usuario SOL secundario, sin el RUC delante («MODDATOS»).
    sol_user: str = ""
    sol_password: str = ""
    # Certificado digital en PEM: la llave privada y el certificado juntos.
    certificate_pem: str = ""
    # Cuándo vence el certificado (UTC), si se pudo leer al subirlo.
    certificate_expires_at: datetime | None = None
    # La del restaurante, no se guarda acá: la fecha de emisión es la del día
    # del local, no la de UTC.
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        self.ruc = self.ruc.strip()
        if self.ruc and not _RUC.fullmatch(self.ruc):
            message = "El RUC del local tiene 11 dígitos y empieza en 10 o 20."
            raise InvalidBillingSettingsError(message)
        self.legal_name = " ".join(self.legal_name.split())[:MAX_CUSTOMER_NAME_LENGTH]
        self.address = " ".join(self.address.split())[:MAX_ADDRESS_LENGTH]
        rate = Decimal(self.igv_rate)
        if rate < 0 or rate > MAX_IGV_PERCENT or rate != rate.quantize(CENT):
            message = "La tasa de IGV va de 0 a 30 % con dos decimales."
            raise InvalidBillingSettingsError(message)
        self.igv_rate = rate.quantize(CENT)
        self.sales_receipt_series = self._series(
            self.sales_receipt_series, InvoiceKind.SALES_RECEIPT
        )
        self.tax_invoice_series = self._series(self.tax_invoice_series, InvoiceKind.TAX_INVOICE)
        self.sales_receipt_credit_series = self._series(
            self.sales_receipt_credit_series, InvoiceKind.SALES_RECEIPT, credit=True
        )
        self.tax_invoice_credit_series = self._series(
            self.tax_invoice_credit_series, InvoiceKind.TAX_INVOICE, credit=True
        )
        if self.sales_receipt_credit_series == self.sales_receipt_series or (
            self.tax_invoice_credit_series == self.tax_invoice_series
        ):
            message = "La serie de las notas de crédito no puede ser la de los comprobantes."
            raise InvalidBillingSettingsError(message)
        self.sol_user = self.sol_user.strip().upper()
        if self.sol_user and not _SOL_USER.fullmatch(self.sol_user):
            message = "El usuario SOL son de 3 a 20 letras o números, sin el RUC delante."
            raise InvalidBillingSettingsError(message)
        if len(self.sol_password) > MAX_SOL_PASSWORD_LENGTH:
            message = "La clave SOL es demasiado larga."
            raise InvalidBillingSettingsError(message)

    @staticmethod
    def _series(raw: str, kind: InvoiceKind, *, credit: bool = False) -> str:
        series = raw.strip().upper()
        if not _SERIES[kind.value].fullmatch(series):
            letter = "B" if kind is InvoiceKind.SALES_RECEIPT else "F"
            subject = f"notas de crédito de {kind.label.lower()}" if credit else kind.label.lower()
            message = f"La serie de {subject} son 4 caracteres y empieza con {letter}."
            raise InvalidBillingSettingsError(message)
        return series

    @property
    def has_sol_credentials(self) -> bool:
        return bool(self.sol_user and self.sol_password)

    @property
    def has_certificate(self) -> bool:
        return bool(self.certificate_pem)

    @property
    def can_register(self) -> bool:
        """Si hay todo lo que el servicio de facturación necesita para firmar por el local."""
        return bool(self.ruc and self.has_sol_credentials and self.has_certificate)

    def certificate_expired(self, now: datetime) -> bool:
        return self.certificate_expires_at is not None and self.certificate_expires_at <= now

    def is_ready(self, now: datetime) -> bool:
        """Si se puede emitir de verdad: datos del local, usuario SOL y certificado vigente."""
        return bool(self.legal_name and self.can_register) and not self.certificate_expired(now)

    def ensure_can_issue(self, kind: InvoiceKind) -> None:
        """Rechaza el tipo de comprobante que el régimen del local no permite."""
        if kind is InvoiceKind.TAX_INVOICE and not self.tax_regime.allows_tax_invoice:
            message = (
                f"El local está en el {self.tax_regime.label}: solo emite boletas. "
                "Cambia el régimen en los datos fiscales para emitir facturas."
            )
            raise InvalidInvoiceError(message)

    def series_for(self, kind: InvoiceKind) -> str:
        return (
            self.sales_receipt_series
            if kind is InvoiceKind.SALES_RECEIPT
            else self.tax_invoice_series
        )

    def credit_series_for(self, kind: InvoiceKind) -> str:
        """La serie de la nota de crédito que afecta a un comprobante de ese tipo."""
        return (
            self.sales_receipt_credit_series
            if kind is InvoiceKind.SALES_RECEIPT
            else self.tax_invoice_credit_series
        )


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    description: str
    quantity: int
    # Precio unitario con IGV, como en la carta.
    unit_price: Decimal
    # Lo que se cobra por la línea: cero en una cortesía.
    total: Decimal
    # Una cortesía va como transferencia gratuita: su precio de carta es solo referencial.
    is_courtesy: bool = False


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
            message = "Una factura necesita el RUC del cliente (11 dígitos)."
            raise InvalidInvoiceError(message)
        if not name:
            message = "Una factura necesita la razón social del cliente."
            raise InvalidInvoiceError(message)
    elif doc is DocumentType.NONE:
        if total > ANONYMOUS_LIMIT:
            message = (
                f"Una boleta de más de S/ {ANONYMOUS_LIMIT} necesita el documento del cliente."
            )
            raise InvalidInvoiceError(message)
        number, name = "", name or "Clientes varios"
    else:
        patterns = {DocumentType.DNI: _DNI, DocumentType.CE: _CE, DocumentType.RUC: _RUC}
        if not patterns[doc].fullmatch(number):
            message = f"El número de {doc.value.upper()} no es válido."
            raise InvalidInvoiceError(message)
        if not name:
            message = "Escribe el nombre del cliente."
            raise InvalidInvoiceError(message)
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
    # Lo último que se envió a SUNAT y lo que respondió, cifrado en reposo: el XML
    # firmado, la constancia (CDR, un ZIP en base64) y el documento que se envió,
    # con el que se vuelve a armar el PDF.
    signed_xml: str = ""
    cdr_zip: str = ""
    document_payload: dict[str, Any] = field(default_factory=dict)
    provider_message: str = ""
    provider_response: dict[str, Any] = field(default_factory=dict)
    archive_status: ArchiveStatus = ArchiveStatus.DISABLED
    archive_message: str = ""
    archived_at: datetime | None = None
    # La anulación: cuándo, por qué y quién. Uno anulado deja de ser el vigente del pedido.
    voided_at: datetime | None = None
    void_reason: str = ""
    voided_by: int | None = None
    # El comprobante anulado del mismo pedido al que este reemplaza.
    replaces_invoice_id: int | None = None
    # La nota de crédito que lo anula, aceptada o en curso. La lee el repositorio de su
    # tabla; guardar el comprobante no la toca.
    credit_note: CreditNote | None = None
    id: int | None = None
    issued_at: datetime = field(default_factory=utc_now)

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
        self,
        status: InvoiceStatus,
        message: str,
        raw: dict[str, Any],
        *,
        signed_xml: str = "",
        cdr_zip: str = "",
        document_payload: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.provider_message = message[:300]
        self.provider_response = raw
        self.signed_xml = signed_xml
        self.cdr_zip = cdr_zip
        self.document_payload = document_payload or {}

    @property
    def has_xml(self) -> bool:
        return bool(self.signed_xml)

    @property
    def has_cdr(self) -> bool:
        return bool(self.cdr_zip)

    @property
    def has_pdf(self) -> bool:
        """El PDF se arma al pedirlo con el documento que se envió a SUNAT.

        Solo de un comprobante aceptado: es la representación impresa que se entrega
        al cliente, y la de uno pendiente o rechazado no vale.
        """
        return self.status is InvoiceStatus.ACCEPTED and bool(self.document_payload)

    @property
    def has_documents_to_archive(self) -> bool:
        """Solo un comprobante aceptado por SUNAT tiene documentos definitivos que guardar."""
        return self.status is InvoiceStatus.ACCEPTED and self.has_xml

    def mark_archive_pending(self) -> None:
        self.archive_status = ArchiveStatus.PENDING
        self.archive_message = ""

    def record_archive(self, status: ArchiveStatus, message: str, archived_at: datetime) -> None:
        self.archive_status = status
        self.archive_message = message[:300]
        self.archived_at = archived_at if status is ArchiveStatus.DONE else None

    def ensure_resendable(self) -> None:
        if self.status is InvoiceStatus.ACCEPTED:
            message = f"El comprobante {self.code} ya fue aceptado por SUNAT."
            raise InvalidInvoiceError(message)
        if self.is_voided:
            message = f"El comprobante {self.code} está anulado: no se reenvía."
            raise InvalidInvoiceError(message)

    @property
    def is_voided(self) -> bool:
        return self.voided_at is not None

    @property
    def needs_credit_note(self) -> bool:
        """Solo uno aceptado tuvo validez ante SUNAT: anularlo exige una nota de crédito.

        Uno rechazado o sin enviar nunca existió para SUNAT y se anula solo en RestHub.
        """
        return self.status is InvoiceStatus.ACCEPTED

    def ensure_voidable(self) -> None:
        if self.is_voided:
            raise InvoiceAlreadyVoidedError(self.code)
        if self.status is InvoiceStatus.PENDING:
            message = (
                f"El comprobante {self.code} está pendiente en SUNAT: reenvíalo y anúlalo "
                "cuando se resuelva."
            )
            raise InvoiceNotVoidableError(message)
        note = self.credit_note
        if note is not None and note.status is not InvoiceStatus.ACCEPTED:
            message = (
                f"La nota de crédito {note.code} de {self.code} sigue en curso "
                f"({note.status.label.lower()}): reenvíala."
            )
            raise InvoiceNotVoidableError(message)

    def void(self, reason: str, voided_by: int, voided_at: datetime) -> None:
        self.voided_at = voided_at
        self.void_reason = reason
        self.voided_by = voided_by


def clean_void_reason(reason: str) -> str:
    """El motivo de una anulación, en una línea; lo exige SUNAT en la nota de crédito."""
    cleaned = " ".join(reason.split())
    if not MIN_VOID_REASON_LENGTH <= len(cleaned) <= MAX_VOID_REASON_LENGTH:
        message = (
            f"Escribe el motivo de la anulación (de {MIN_VOID_REASON_LENGTH} a "
            f"{MAX_VOID_REASON_LENGTH} caracteres)."
        )
        raise InvalidInvoiceError(message)
    return cleaned


def build_lines(items: Sequence[tuple[str, int, Decimal, bool]]) -> tuple[InvoiceLine, ...]:
    """Las líneas del comprobante desde los ítems del pedido.

    Las cortesías no se cobran: van como transferencia gratuita (total cero),
    con su precio de carta como valor referencial, como pide SUNAT.
    """
    return tuple(
        InvoiceLine(
            description=name[:250],
            quantity=quantity,
            unit_price=unit_price.quantize(CENT),
            total=ZERO if is_courtesy else (unit_price * quantity).quantize(CENT),
            is_courtesy=is_courtesy,
        )
        for name, quantity, unit_price, is_courtesy in items
    )
