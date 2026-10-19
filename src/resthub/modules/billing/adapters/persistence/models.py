from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base
from resthub.core.encrypted_types import EncryptedText


class BillingSettingsRow(Base):
    __tablename__ = "billing_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Uno por restaurante.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_billing_settings_restaurant", ondelete="RESTRICT"),
        unique=True,
    )
    ruc: Mapped[str] = mapped_column(String(11), default="")
    legal_name: Mapped[str] = mapped_column(String(100), default="")
    address: Mapped[str] = mapped_column(String(200), default="")
    igv_rate: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=2))
    sales_receipt_series: Mapped[str] = mapped_column("boleta_series", String(4))
    tax_invoice_series: Mapped[str] = mapped_column("factura_series", String(4))
    # Las series de las notas de crédito que anulan boletas y facturas.
    sales_receipt_credit_series: Mapped[str] = mapped_column(
        "boleta_credit_note_series", String(4), default="BC01", server_default="BC01"
    )
    tax_invoice_credit_series: Mapped[str] = mapped_column(
        "factura_credit_note_series", String(4), default="FC01", server_default="FC01"
    )
    # Credenciales de SUNAT del local, cifradas en reposo. Nunca se devuelven por el API
    # ni se escriben en logs.
    sol_user: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    sol_password: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    # La llave privada y el certificado en PEM.
    certificate_pem: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    certificate_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class InvoiceRow(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_invoices_restaurant", ondelete="RESTRICT")
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", name="fk_invoices_order", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(10))
    series: Mapped[str] = mapped_column(String(4))
    number: Mapped[int] = mapped_column(Integer)
    customer_document_type: Mapped[str] = mapped_column(String(10))
    customer_document_number: Mapped[str] = mapped_column(EncryptedText, default="")
    customer_name: Mapped[str] = mapped_column(String(100), default="")
    customer_address: Mapped[str] = mapped_column(EncryptedText, default="")
    # Las líneas congeladas al emitir: [{description, quantity, unit_price, total}].
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    total: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    discount: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    igv_rate: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=2))
    status: Mapped[str] = mapped_column(String(20))
    # Lo que se envió a SUNAT y lo que respondió, cifrado (llevan el documento del cliente):
    # el XML firmado, la constancia (CDR, ZIP en base64) y el documento en JSON, con el
    # que se vuelve a armar el PDF.
    signed_xml: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    cdr_zip: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    document_payload: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    # Respaldo del PDF, el XML y el CDR en el almacenamiento propio (ver `ArchiveStatus`).
    archive_status: Mapped[str] = mapped_column(
        String(20), default="disabled", server_default="disabled"
    )
    archive_message: Mapped[str] = mapped_column(String(300), default="", server_default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_message: Mapped[str] = mapped_column(String(300), default="")
    provider_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    issued_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_invoices_issued_by", ondelete="RESTRICT")
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # La anulación: con una nota de crédito aceptada o, si nunca valió ante SUNAT, solo acá.
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    voided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_invoices_voided_by", ondelete="RESTRICT"), nullable=True
    )
    # El comprobante anulado del mismo pedido al que este reemplaza.
    replaces_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", name="fk_invoices_replaces", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        # El correlativo de cada serie no se repite en el local.
        UniqueConstraint("restaurant_id", "series", "number", name="uq_invoices_series_number"),
        # Un pedido, a lo sumo un comprobante vigente: los anulados no cuentan. El caso
        # de uso lo revisa antes; el índice parcial es la última palabra.
        Index(
            "uq_invoices_order_current",
            "restaurant_id",
            "order_id",
            unique=True,
            sqlite_where=text("voided_at IS NULL"),
            postgresql_where=text("voided_at IS NULL"),
        ),
        Index("ix_invoices_restaurant_issued", "restaurant_id", "issued_at"),
    )


class CreditNoteRow(Base):
    __tablename__ = "credit_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_credit_notes_restaurant", ondelete="RESTRICT")
    )
    # Una nota por comprobante: si SUNAT no la acepta se reenvía la misma.
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", name="fk_credit_notes_invoice", ondelete="RESTRICT"),
        unique=True,
    )
    series: Mapped[str] = mapped_column(String(4))
    number: Mapped[int] = mapped_column(Integer)
    # Catálogo 09 de SUNAT: 01 anulación de la operación.
    reason_code: Mapped[str] = mapped_column(String(2))
    reason: Mapped[str] = mapped_column(String(200))
    total: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    status: Mapped[str] = mapped_column(String(20))
    # Como en `invoices`: cifrados porque llevan el documento del cliente.
    signed_xml: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    cdr_zip: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    document_payload: Mapped[str] = mapped_column(EncryptedText, default="", server_default="")
    provider_message: Mapped[str] = mapped_column(String(300), default="")
    provider_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    issued_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_credit_notes_issued_by", ondelete="RESTRICT")
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("restaurant_id", "series", "number", name="uq_credit_notes_series_number"),
    )
