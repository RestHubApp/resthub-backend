from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, Numeric, String, column, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.local_time import DEFAULT_TIMEZONE, local_midnight
from resthub.core.pagination import Page
from resthub.core.timestamps import as_utc
from resthub.modules.billing.adapters.persistence.models import BillingSettingsRow, InvoiceRow
from resthub.modules.billing.domain.exceptions import InvoiceNotFound, InvoiceNumberTaken
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceLine,
    InvoiceStatus,
)
from resthub.modules.billing.ports.billing_ports import InvoiceQuery, PaidOrder

_restaurants = table("restaurants", column("id", Integer), column("timezone", String))


async def _timezone(session: AsyncSession, restaurant_id: int) -> str:
    zone = await session.scalar(
        select(_restaurants.c.timezone).where(_restaurants.c.id == restaurant_id)
    )
    return str(zone) if zone else DEFAULT_TIMEZONE


def _settings(row: BillingSettingsRow, timezone: str) -> BillingSettings:
    return BillingSettings(
        restaurant_id=row.restaurant_id,
        ruc=row.ruc,
        legal_name=row.legal_name,
        address=row.address,
        igv_rate=row.igv_rate,
        boleta_series=row.boleta_series,
        factura_series=row.factura_series,
        provider_url=row.provider_url,
        provider_token=row.provider_token,
        timezone=timezone,
    )


class SqlAlchemyBillingSettings:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, restaurant_id: int) -> BillingSettings:
        row = await self._row(restaurant_id)
        timezone = await _timezone(self._session, restaurant_id)
        if row is None:
            return BillingSettings(restaurant_id=restaurant_id, timezone=timezone)
        return _settings(row, timezone)

    async def save(self, settings: BillingSettings) -> BillingSettings:
        row = await self._row(settings.restaurant_id)
        if row is None:
            row = BillingSettingsRow(restaurant_id=settings.restaurant_id)
            self._session.add(row)
        row.ruc = settings.ruc
        row.legal_name = settings.legal_name
        row.address = settings.address
        row.igv_rate = settings.igv_rate
        row.boleta_series = settings.boleta_series
        row.factura_series = settings.factura_series
        row.provider_url = settings.provider_url
        row.provider_token = settings.provider_token
        await self._session.flush()
        return _settings(row, settings.timezone)

    async def _row(self, restaurant_id: int) -> BillingSettingsRow | None:
        return (
            await self._session.execute(
                select(BillingSettingsRow).where(BillingSettingsRow.restaurant_id == restaurant_id)
            )
        ).scalar_one_or_none()


def _invoice(row: InvoiceRow) -> Invoice:
    return Invoice(
        id=row.id,
        restaurant_id=row.restaurant_id,
        order_id=row.order_id,
        kind=InvoiceKind(row.kind),
        series=row.series,
        number=row.number,
        customer=Customer(
            document_type=DocumentType(row.customer_document_type),
            document_number=row.customer_document_number,
            name=row.customer_name,
            address=row.customer_address,
        ),
        lines=tuple(
            InvoiceLine(
                description=str(line["description"]),
                quantity=int(line["quantity"]),
                unit_price=Decimal(str(line["unit_price"])),
                total=Decimal(str(line["total"])),
            )
            for line in row.lines or []
        ),
        total=row.total,
        discount=row.discount,
        igv_rate=row.igv_rate,
        issued_by=row.issued_by,
        status=InvoiceStatus(row.status),
        pdf_url=row.pdf_url,
        provider_message=row.provider_message,
        provider_response=row.provider_response or {},
        issued_at=as_utc(row.issued_at),
    )


def _copy(invoice: Invoice, row: InvoiceRow) -> None:
    row.restaurant_id = invoice.restaurant_id
    row.order_id = invoice.order_id
    row.kind = invoice.kind.value
    row.series = invoice.series
    row.number = invoice.number
    row.customer_document_type = invoice.customer.document_type.value
    row.customer_document_number = invoice.customer.document_number
    row.customer_name = invoice.customer.name
    row.customer_address = invoice.customer.address
    row.lines = [
        {
            "description": line.description,
            "quantity": line.quantity,
            "unit_price": str(line.unit_price),
            "total": str(line.total),
        }
        for line in invoice.lines
    ]
    row.total = invoice.total
    row.discount = invoice.discount
    row.igv_rate = invoice.igv_rate
    row.status = invoice.status.value
    row.pdf_url = invoice.pdf_url
    row.provider_message = invoice.provider_message
    row.provider_response = invoice.provider_response
    row.issued_by = invoice.issued_by
    row.issued_at = invoice.issued_at


class SqlAlchemyInvoiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, invoice: Invoice) -> Invoice:
        row = InvoiceRow()
        _copy(invoice, row)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Solo lo disparan los índices únicos del número y del pedido.
            await self._session.rollback()
            raise InvoiceNumberTaken() from error
        return _invoice(row)

    async def get(self, restaurant_id: int, invoice_id: int) -> Invoice | None:
        row = await self._row(restaurant_id, invoice_id)
        return _invoice(row) if row else None

    async def for_order(self, restaurant_id: int, order_id: int) -> Invoice | None:
        row = (
            await self._session.execute(
                select(InvoiceRow).where(
                    InvoiceRow.restaurant_id == restaurant_id, InvoiceRow.order_id == order_id
                )
            )
        ).scalar_one_or_none()
        return _invoice(row) if row else None

    async def save(self, invoice: Invoice) -> Invoice:
        row = await self._row(invoice.restaurant_id, invoice.id or 0)
        if row is None:
            raise InvoiceNotFound(invoice.id or 0)
        _copy(invoice, row)
        await self._session.flush()
        return _invoice(row)

    async def search(self, query: InvoiceQuery) -> Page[Invoice]:
        base = select(InvoiceRow).where(InvoiceRow.restaurant_id == query.restaurant_id)
        if query.status is not None:
            base = base.where(InvoiceRow.status == query.status.value)
        if query.date_from is not None or query.date_to is not None:
            # Los días son los del local: una boleta de las 21:00 en Lima es de
            # hoy, aunque en UTC ya sea mañana.
            timezone = await _timezone(self._session, query.restaurant_id)
            if query.date_from is not None:
                base = base.where(InvoiceRow.issued_at >= local_midnight(query.date_from, timezone))
            if query.date_to is not None:
                base = base.where(
                    InvoiceRow.issued_at
                    < local_midnight(query.date_to + timedelta(days=1), timezone)
                )
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        rows = await self._session.scalars(
            base.order_by(InvoiceRow.issued_at.desc(), InvoiceRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        return Page(items=[_invoice(row) for row in rows], total=total)

    async def next_number(self, restaurant_id: int, series: str) -> int:
        # La fila del restaurante hace de turno: dos cobros que emiten a la
        # vez no sacan el mismo número. No sirve la de datos fiscales porque un
        # local que todavía no los cargó no la tiene, y `FOR UPDATE` sobre una
        # fila que no existe no bloquea nada. El índice único es la última palabra.
        await self._session.execute(
            select(_restaurants.c.id).where(_restaurants.c.id == restaurant_id).with_for_update()
        )
        last = await self._session.scalar(
            select(func.max(InvoiceRow.number)).where(
                InvoiceRow.restaurant_id == restaurant_id, InvoiceRow.series == series
            )
        )
        return int(last or 0) + 1

    async def _row(self, restaurant_id: int, invoice_id: int) -> InvoiceRow | None:
        return (
            await self._session.execute(
                select(InvoiceRow).where(
                    InvoiceRow.id == invoice_id, InvoiceRow.restaurant_id == restaurant_id
                )
            )
        ).scalar_one_or_none()


# -- Pedidos pagados, leídos de las tablas de `orders` -------------------------

_orders = table(
    "orders",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("number", Integer),
    column("status", String),
    column("total", Numeric(10, 2)),
    column("discount_amount", Numeric(10, 2)),
)
_order_items = table(
    "order_items",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("order_id", Integer),
    column("name", String),
    column("quantity", Integer),
    column("unit_price", Numeric(10, 2)),
    column("is_courtesy", Boolean),
    column("modifiers", JSON),
)


def _description(name: str, modifiers: Any) -> str:
    options = [str(m.get("option", "")) for m in modifiers or [] if isinstance(m, dict)]
    return f"{name} ({', '.join(options)})" if options else name


class SqlPaidOrderDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, restaurant_id: int, order_id: int) -> PaidOrder | None:
        order = (
            await self._session.execute(
                select(_orders).where(
                    _orders.c.id == order_id,
                    _orders.c.restaurant_id == restaurant_id,
                    _orders.c.status == "paid",
                )
            )
        ).one_or_none()
        if order is None:
            return None
        items = await self._session.execute(
            select(
                _order_items.c.name,
                _order_items.c.quantity,
                _order_items.c.unit_price,
                _order_items.c.is_courtesy,
                _order_items.c.modifiers,
            )
            .where(
                _order_items.c.restaurant_id == restaurant_id,
                _order_items.c.order_id == order_id,
            )
            .order_by(_order_items.c.id)
        )
        return PaidOrder(
            id=int(order.id),
            number=int(order.number),
            total=Decimal(str(order.total)),
            discount=Decimal(str(order.discount_amount or 0)),
            items=tuple(
                (
                    _description(str(row.name), row.modifiers),
                    int(row.quantity),
                    Decimal(str(row.unit_price)),
                    bool(row.is_courtesy),
                )
                for row in items
            ),
        )
