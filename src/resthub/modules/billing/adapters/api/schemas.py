"""Contrato HTTP de los comprobantes y de los datos fiscales."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field

from resthub.core.api_params import EntityId
from resthub.modules.billing.domain.credit_notes import CreditNote, CreditNoteReason
from resthub.modules.billing.domain.invoices import (
    MAX_ADDRESS_LENGTH,
    MAX_CUSTOMER_NAME_LENGTH,
    MAX_SOL_PASSWORD_LENGTH,
    MAX_VOID_REASON_LENGTH,
    ArchiveStatus,
    BillingSettings,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
)


class BillingSettingsResponse(BaseModel):
    ruc: str
    legal_name: str
    address: str
    igv_rate: Decimal
    sales_receipt_series: str = Field(serialization_alias="boleta_series")
    tax_invoice_series: str = Field(serialization_alias="factura_series")
    # Las series de las notas de crédito que anulan boletas y facturas (BC01 y FC01).
    sales_receipt_credit_series: str = Field(serialization_alias="boleta_credit_note_series")
    tax_invoice_credit_series: str = Field(serialization_alias="factura_credit_note_series")
    # El usuario, la clave SOL y el certificado no salen nunca: solo si están cargados.
    has_sol_credentials: bool
    has_certificate: bool
    # Cuándo vence el certificado (UTC), si se pudo leer.
    certificate_expires_at: datetime | None
    # Si los comprobantes se envían de verdad a SUNAT: falso también con el certificado vencido.
    is_ready: bool

    @classmethod
    def from_entity(cls, settings: BillingSettings, now: datetime) -> BillingSettingsResponse:
        return cls(
            ruc=settings.ruc,
            legal_name=settings.legal_name,
            address=settings.address,
            igv_rate=settings.igv_rate,
            sales_receipt_series=settings.sales_receipt_series,
            tax_invoice_series=settings.tax_invoice_series,
            sales_receipt_credit_series=settings.sales_receipt_credit_series,
            tax_invoice_credit_series=settings.tax_invoice_credit_series,
            has_sol_credentials=settings.has_sol_credentials,
            has_certificate=settings.has_certificate,
            certificate_expires_at=settings.certificate_expires_at,
            is_ready=settings.is_ready(now),
        )


class BillingSettingsRequest(BaseModel):
    ruc: str = Field(default="", max_length=11)
    legal_name: str = Field(default="", max_length=MAX_CUSTOMER_NAME_LENGTH)
    address: str = Field(default="", max_length=MAX_ADDRESS_LENGTH)
    igv_rate: Decimal = Field(ge=0, le=30, max_digits=5, decimal_places=2)
    sales_receipt_series: str = Field(validation_alias="boleta_series", min_length=4, max_length=4)
    tax_invoice_series: str = Field(validation_alias="factura_series", min_length=4, max_length=4)
    # Usuario SOL secundario (sin el RUC delante) y su clave. Sin los campos quedan como
    # estaban; vacíos los borran. Van juntos.
    sol_user: str | None = Field(default=None, max_length=20)
    sol_password: str | None = Field(default=None, max_length=MAX_SOL_PASSWORD_LENGTH)
    # Las series de las notas de crédito. Sin los campos quedan como estaban.
    sales_receipt_credit_series: str | None = Field(
        default=None, validation_alias="boleta_credit_note_series", min_length=4, max_length=4
    )
    tax_invoice_credit_series: str | None = Field(
        default=None, validation_alias="factura_credit_note_series", min_length=4, max_length=4
    )


# Un .pfx de SUNAT pesa unos KB; el tope deja margen sin aceptar cualquier archivo.
MAX_CERTIFICATE_BYTES = 64 * 1024


class CertificateUploadRequest(BaseModel):
    # El archivo .pfx, .p12 o .pem en base64.
    content_base64: str = Field(min_length=1, max_length=(MAX_CERTIFICATE_BYTES * 4) // 3 + 4)
    # La contraseña del .pfx/.p12 (vacía para un .pem sin contraseña). No se guarda.
    password: str = Field(default="", max_length=200)


class IssueInvoiceRequest(BaseModel):
    order_id: EntityId
    kind: InvoiceKind
    customer_document_type: DocumentType = DocumentType.NONE
    customer_document_number: str = Field(default="", max_length=15)
    customer_name: str = Field(default="", max_length=MAX_CUSTOMER_NAME_LENGTH)
    customer_address: str = Field(default="", max_length=MAX_ADDRESS_LENGTH)


class InvoiceLineResponse(BaseModel):
    description: str
    quantity: int
    unit_price: Decimal
    total: Decimal
    # Una cortesía: va como transferencia gratuita, con total cero.
    is_courtesy: bool


class CreditNoteResponse(BaseModel):
    id: int
    # El comprobante que anula.
    invoice_id: int
    code: str
    series: str
    number: int
    # Catálogo 09 de SUNAT: «01» anulación de la operación.
    reason_code: CreditNoteReason
    reason_label: str
    # El motivo que escribió el encargado.
    reason: str
    total: Decimal
    status: InvoiceStatus
    status_label: str
    provider_message: str
    # Qué documentos se pueden descargar (`GET /billing/credit-notes/{id}/pdf|xml|cdr`).
    has_pdf: bool
    has_xml: bool
    has_cdr: bool
    issued_at: datetime

    @classmethod
    def from_entity(cls, note: CreditNote) -> CreditNoteResponse:
        return cls(
            id=note.id or 0,
            invoice_id=note.invoice_id,
            code=note.code,
            series=note.series,
            number=note.number,
            reason_code=note.reason_code,
            reason_label=note.reason_code.label,
            reason=note.reason,
            total=note.total,
            status=note.status,
            status_label=note.status.label,
            provider_message=note.provider_message,
            has_pdf=note.has_pdf,
            has_xml=note.has_xml,
            has_cdr=note.has_cdr,
            issued_at=note.issued_at,
        )


class VoidInvoiceRequest(BaseModel):
    # Va a SUNAT en la nota de crédito: «Cliente pidió factura», «RUC errado»…
    reason: str = Field(max_length=MAX_VOID_REASON_LENGTH)


class InvoiceResponse(BaseModel):
    id: int
    order_id: int
    kind: InvoiceKind
    kind_label: str
    code: str
    series: str
    number: int
    customer_document_type: DocumentType
    customer_document_number: str
    customer_name: str
    customer_address: str
    lines: list[InvoiceLineResponse]
    discount: Decimal
    # Base imponible, IGV y total (con IGV).
    taxable: Decimal
    igv: Decimal
    igv_rate: Decimal
    total: Decimal
    status: InvoiceStatus
    status_label: str
    provider_message: str
    # Qué documentos se pueden descargar (`GET /billing/invoices/{id}/pdf|xml|cdr`).
    has_pdf: bool
    has_xml: bool
    has_cdr: bool
    # Respaldo en Google Drive: disabled (no aplica), pending, done o failed.
    archive_status: ArchiveStatus
    archive_status_label: str
    archive_message: str
    archived_at: datetime | None
    issued_at: datetime
    # La anulación: un comprobante anulado deja emitir otro para el mismo pedido.
    is_voided: bool
    voided_at: datetime | None
    void_reason: str
    # La nota de crédito que lo anula (aceptada) o que está en curso (pendiente o
    # rechazada: el comprobante sigue vigente hasta que SUNAT la acepte).
    credit_note_id: int | None
    credit_note: CreditNoteResponse | None
    # El comprobante anulado del mismo pedido al que este reemplaza.
    replaces_invoice_id: int | None

    @classmethod
    def from_entity(cls, invoice: Invoice) -> InvoiceResponse:
        note = invoice.credit_note
        return cls(
            id=invoice.id or 0,
            order_id=invoice.order_id,
            kind=invoice.kind,
            kind_label=invoice.kind.label,
            code=invoice.code,
            series=invoice.series,
            number=invoice.number,
            customer_document_type=invoice.customer.document_type,
            customer_document_number=invoice.customer.document_number,
            customer_name=invoice.customer.name,
            customer_address=invoice.customer.address,
            lines=[
                InvoiceLineResponse(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    total=line.total,
                    is_courtesy=line.is_courtesy,
                )
                for line in invoice.lines
            ],
            discount=invoice.discount,
            taxable=invoice.taxable,
            igv=invoice.igv,
            igv_rate=invoice.igv_rate,
            total=invoice.total,
            status=invoice.status,
            status_label=invoice.status.label,
            provider_message=invoice.provider_message,
            has_pdf=invoice.has_pdf,
            has_xml=invoice.has_xml,
            has_cdr=invoice.has_cdr,
            archive_status=invoice.archive_status,
            archive_status_label=invoice.archive_status.label,
            archive_message=invoice.archive_message,
            archived_at=invoice.archived_at,
            issued_at=invoice.issued_at,
            is_voided=invoice.is_voided,
            voided_at=invoice.voided_at,
            void_reason=invoice.void_reason,
            credit_note_id=note.id if note else None,
            credit_note=CreditNoteResponse.from_entity(note) if note else None,
            replaces_invoice_id=invoice.replaces_invoice_id,
        )


class InvoicePageResponse(BaseModel):
    items: list[InvoiceResponse]
    total: int


class SendInvoiceEmailRequest(BaseModel):
    # El correo del cliente, confirmado por quien envía (puede venir prellenado de su ficha).
    to_email: EmailStr = Field(max_length=254)
    to_name: str = Field(default="", max_length=MAX_CUSTOMER_NAME_LENGTH)


class SendInvoiceEmailResponse(BaseModel):
    sent: bool
    # El correo enmascarado (`a•••@correo.pe`): el servidor no repite la dirección completa.
    masked_to: str


class BillingCapabilitiesResponse(BaseModel):
    """Qué funciones opcionales tiene encendidas el servidor; la pantalla oculta lo apagado."""

    email_enabled: bool
    email_max_per_hour: int
    archive_enabled: bool
