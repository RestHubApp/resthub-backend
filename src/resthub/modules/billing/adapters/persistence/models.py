from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base


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
    boleta_series: Mapped[str] = mapped_column(String(4))
    factura_series: Mapped[str] = mapped_column(String(4))
    provider_url: Mapped[str] = mapped_column(String(300), default="")
    # El token del proveedor. Nunca se devuelve por el API ni se escribe en logs.
    provider_token: Mapped[str] = mapped_column(String(300), default="")


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
    customer_document_number: Mapped[str] = mapped_column(String(15), default="")
    customer_name: Mapped[str] = mapped_column(String(100), default="")
    customer_address: Mapped[str] = mapped_column(String(200), default="")
    # Las líneas congeladas al emitir: [{description, quantity, unit_price, total}].
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    total: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    discount: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    igv_rate: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=2))
    status: Mapped[str] = mapped_column(String(20))
    pdf_url: Mapped[str] = mapped_column(String(500), default="")
    provider_message: Mapped[str] = mapped_column(String(300), default="")
    provider_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    issued_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_invoices_issued_by", ondelete="RESTRICT")
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # El correlativo de cada serie no se repite en el local.
        UniqueConstraint("restaurant_id", "series", "number", name="uq_invoices_series_number"),
        # Un pedido, un comprobante.
        UniqueConstraint("restaurant_id", "order_id", name="uq_invoices_order"),
        Index("ix_invoices_restaurant_issued", "restaurant_id", "issued_at"),
    )
