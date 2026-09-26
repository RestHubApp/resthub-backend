"""Contrato HTTP de los comprobantes y de los datos fiscales."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from resthub.modules.billing.domain.invoices import (
    MAX_ADDRESS_LENGTH,
    MAX_CUSTOMER_NAME_LENGTH,
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
    boleta_series: str
    factura_series: str
    provider_url: str
    # El token no sale nunca; solo si está cargado.
    has_provider_token: bool
    # Si los comprobantes se envían de verdad a SUNAT.
    is_ready: bool

    @classmethod
    def from_entity(cls, settings: BillingSettings) -> BillingSettingsResponse:
        return cls(
            ruc=settings.ruc,
            legal_name=settings.legal_name,
            address=settings.address,
            igv_rate=settings.igv_rate,
            boleta_series=settings.boleta_series,
            factura_series=settings.factura_series,
            provider_url=settings.provider_url,
            has_provider_token=bool(settings.provider_token),
            is_ready=settings.is_ready,
        )


class BillingSettingsRequest(BaseModel):
    ruc: str = Field(default="", max_length=11)
    legal_name: str = Field(default="", max_length=MAX_CUSTOMER_NAME_LENGTH)
    address: str = Field(default="", max_length=MAX_ADDRESS_LENGTH)
    igv_rate: Decimal = Field(ge=0, le=30, max_digits=5, decimal_places=2)
    boleta_series: str = Field(min_length=4, max_length=4)
    factura_series: str = Field(min_length=4, max_length=4)
    provider_url: str = Field(default="", max_length=300)
    # Sin el campo, el token queda como estaba; vacío lo borra.
    provider_token: str | None = Field(default=None, max_length=300)


class IssueInvoiceRequest(BaseModel):
    order_id: int = Field(ge=1)
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
    pdf_url: str
    issued_at: datetime

    @classmethod
    def from_entity(cls, invoice: Invoice) -> InvoiceResponse:
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
            pdf_url=invoice.pdf_url,
            issued_at=invoice.issued_at,
        )


class InvoicePageResponse(BaseModel):
    items: list[InvoiceResponse]
    total: int
