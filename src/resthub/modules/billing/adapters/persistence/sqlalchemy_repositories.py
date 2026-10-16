from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    case,
    column,
    func,
    select,
    table,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.local_time import DEFAULT_TIMEZONE, local_midnight
from resthub.core.pagination import Page
from resthub.core.timestamps import as_utc
from resthub.modules.billing.adapters.persistence.models import (
    BillingSettingsRow,
    CreditNoteRow,
    InvoiceRow,
)
from resthub.modules.billing.domain.credit_notes import CreditNote, CreditNoteReason
from resthub.modules.billing.domain.exceptions import (
    CreditNoteNotFoundError,
    InvoiceNotFoundError,
    InvoiceNumberTakenError,
)
from resthub.modules.billing.domain.invoices import (
    CENT,
    ArchiveStatus,
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceLine,
    InvoiceStatus,
)
from resthub.modules.billing.ports.billing_ports import InvoiceQuery, PaidOrder

_restaurants = table(
    "restaurants",
    column("id", Integer),
    column("timezone", String),
    column("is_sandbox", Boolean),
)


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
        sales_receipt_series=row.sales_receipt_series,
        tax_invoice_series=row.tax_invoice_series,
        sales_receipt_credit_series=row.sales_receipt_credit_series,
        tax_invoice_credit_series=row.tax_invoice_credit_series,
        sol_user=row.sol_user,
        sol_password=row.sol_password,
        certificate_pem=row.certificate_pem,
        certificate_expires_at=as_utc(row.certificate_expires_at)
        if row.certificate_expires_at
        else None,
        timezone=timezone,
    )


class SqlAlchemySandboxDirectory:
    """Lee `restaurants.is_sandbox`; la tabla es de `restaurants` y acá no se escribe."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_sandbox(self, restaurant_id: int) -> bool:
        flag = await self._session.scalar(
            select(_restaurants.c.is_sandbox).where(_restaurants.c.id == restaurant_id)
        )
        return bool(flag)


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
        # Tomada: dos primeros guardados a la vez no chocan al crear la fila.
        row = await locked_settings_row(self._session, settings.restaurant_id)
        row.ruc = settings.ruc
        row.legal_name = settings.legal_name
        row.address = settings.address
        row.igv_rate = settings.igv_rate
        row.sales_receipt_series = settings.sales_receipt_series
        row.tax_invoice_series = settings.tax_invoice_series
        row.sales_receipt_credit_series = settings.sales_receipt_credit_series
        row.tax_invoice_credit_series = settings.tax_invoice_credit_series
        row.sol_user = settings.sol_user
        row.sol_password = settings.sol_password
        row.certificate_pem = settings.certificate_pem
        row.certificate_expires_at = settings.certificate_expires_at
        await self._session.flush()
        return _settings(row, settings.timezone)

    async def _row(self, restaurant_id: int) -> BillingSettingsRow | None:
        return (
            await self._session.execute(
                select(BillingSettingsRow).where(BillingSettingsRow.restaurant_id == restaurant_id)
            )
        ).scalar_one_or_none()


def _invoice(row: InvoiceRow, note: CreditNote | None = None) -> Invoice:
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
                is_courtesy=bool(line.get("is_courtesy", False)),
            )
            for line in row.lines or []
        ),
        total=row.total,
        discount=row.discount,
        igv_rate=row.igv_rate,
        issued_by=row.issued_by,
        status=InvoiceStatus(row.status),
        signed_xml=row.signed_xml or "",
        cdr_zip=row.cdr_zip or "",
        document_payload=_documents(row.document_payload),
        provider_message=row.provider_message,
        provider_response=row.provider_response or {},
        archive_status=ArchiveStatus(row.archive_status),
        archive_message=row.archive_message,
        archived_at=as_utc(row.archived_at) if row.archived_at else None,
        voided_at=as_utc(row.voided_at) if row.voided_at else None,
        void_reason=row.void_reason,
        voided_by=row.voided_by,
        replaces_invoice_id=row.replaces_invoice_id,
        credit_note=note,
        issued_at=as_utc(row.issued_at),
    )


def _documents(payload: str) -> dict[str, Any]:
    return json.loads(payload) if payload else {}


def _payload_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) if payload else ""


def _credit_note(row: CreditNoteRow) -> CreditNote:
    return CreditNote(
        id=row.id,
        restaurant_id=row.restaurant_id,
        invoice_id=row.invoice_id,
        series=row.series,
        number=row.number,
        reason_code=CreditNoteReason(row.reason_code),
        reason=row.reason,
        total=row.total,
        issued_by=row.issued_by,
        status=InvoiceStatus(row.status),
        signed_xml=row.signed_xml or "",
        cdr_zip=row.cdr_zip or "",
        document_payload=_documents(row.document_payload),
        provider_message=row.provider_message,
        provider_response=row.provider_response or {},
        issued_at=as_utc(row.issued_at),
    )


def _copy_note(note: CreditNote, row: CreditNoteRow) -> None:
    row.restaurant_id = note.restaurant_id
    row.invoice_id = note.invoice_id
    row.series = note.series
    row.number = note.number
    row.reason_code = note.reason_code.value
    row.reason = note.reason
    row.total = note.total
    row.status = note.status.value
    row.signed_xml = note.signed_xml
    row.cdr_zip = note.cdr_zip
    row.document_payload = _payload_text(note.document_payload)
    row.provider_message = note.provider_message
    row.provider_response = note.provider_response
    row.issued_by = note.issued_by
    row.issued_at = note.issued_at


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
            "is_courtesy": line.is_courtesy,
        }
        for line in invoice.lines
    ]
    row.total = invoice.total
    row.discount = invoice.discount
    row.igv_rate = invoice.igv_rate
    row.status = invoice.status.value
    row.signed_xml = invoice.signed_xml
    row.cdr_zip = invoice.cdr_zip
    row.document_payload = _payload_text(invoice.document_payload)
    row.archive_status = invoice.archive_status.value
    row.archive_message = invoice.archive_message
    row.archived_at = invoice.archived_at
    row.provider_message = invoice.provider_message
    row.provider_response = invoice.provider_response
    row.issued_by = invoice.issued_by
    row.issued_at = invoice.issued_at
    row.voided_at = invoice.voided_at
    row.void_reason = invoice.void_reason
    row.voided_by = invoice.voided_by
    row.replaces_invoice_id = invoice.replaces_invoice_id


async def locked_settings_row(session: AsyncSession, restaurant_id: int) -> BillingSettingsRow:
    """La fila de datos fiscales del local, tomada hasta el fin de la transacción.

    Si el local todavía no la tiene, la crea con los valores por omisión: así
    siempre hay una fila que tomar. Dos que la crean a la vez chocan en el
    índice único dentro de un savepoint, y el segundo lee la del primero.
    """
    statement = (
        select(BillingSettingsRow)
        .where(BillingSettingsRow.restaurant_id == restaurant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(statement)).scalar_one_or_none()
    if row is not None:
        return row
    defaults = BillingSettings(restaurant_id=restaurant_id)
    try:
        async with session.begin_nested():
            session.add(
                BillingSettingsRow(
                    restaurant_id=restaurant_id,
                    igv_rate=defaults.igv_rate,
                    sales_receipt_series=defaults.sales_receipt_series,
                    tax_invoice_series=defaults.tax_invoice_series,
                )
            )
    except IntegrityError:
        pass
    return (await session.execute(statement)).scalar_one()


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
            raise InvoiceNumberTakenError from error
        return _invoice(row)

    async def get(
        self, restaurant_id: int, invoice_id: int, *, for_update: bool = False
    ) -> Invoice | None:
        row = await self._row(restaurant_id, invoice_id, for_update=for_update)
        return (await self._with_notes([row]))[0] if row else None

    async def for_order(self, restaurant_id: int, order_id: int) -> Invoice | None:
        row = (
            await self._session.execute(
                select(InvoiceRow)
                .where(InvoiceRow.restaurant_id == restaurant_id, InvoiceRow.order_id == order_id)
                # El vigente primero; si todos están anulados, el último emitido.
                .order_by(
                    case((InvoiceRow.voided_at.is_(None), 0), else_=1),
                    InvoiceRow.issued_at.desc(),
                    InvoiceRow.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return (await self._with_notes([row]))[0] if row else None

    async def save(self, invoice: Invoice) -> Invoice:
        row = await self._row(invoice.restaurant_id, invoice.id or 0)
        if row is None:
            raise InvoiceNotFoundError(invoice.id or 0)
        _copy(invoice, row)
        await self._session.flush()
        return (await self._with_notes([row]))[0]

    async def _with_notes(self, rows: Sequence[InvoiceRow]) -> list[Invoice]:
        """Los comprobantes con su nota de crédito, leídas todas en una consulta."""
        ids = [row.id for row in rows]
        notes: dict[int, CreditNote] = {}
        if ids:
            found = await self._session.scalars(
                select(CreditNoteRow).where(CreditNoteRow.invoice_id.in_(ids))
            )
            notes = {note.invoice_id: _credit_note(note) for note in found}
        return [_invoice(row, notes.get(row.id)) for row in rows]

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
        return Page(items=await self._with_notes(list(rows)), total=total)

    async def next_number(self, restaurant_id: int, series: str) -> int:
        # La fila de datos fiscales hace de turno: dos cobros que emiten a la
        # vez no sacan el mismo número. No se usa la del restaurante: el turno
        # dura hasta que responde el proveedor, y esa fila es la que numera los
        # pedidos, así que emitir una boleta frenaría la toma de pedidos. El
        # índice único es la última palabra.
        await locked_settings_row(self._session, restaurant_id)
        last = await self._session.scalar(
            select(func.max(InvoiceRow.number)).where(
                InvoiceRow.restaurant_id == restaurant_id, InvoiceRow.series == series
            )
        )
        return int(last or 0) + 1

    async def _row(
        self, restaurant_id: int, invoice_id: int, *, for_update: bool = False
    ) -> InvoiceRow | None:
        statement = select(InvoiceRow).where(
            InvoiceRow.id == invoice_id, InvoiceRow.restaurant_id == restaurant_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(statement)).scalar_one_or_none()


class SqlAlchemyCreditNoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, note: CreditNote) -> CreditNote:
        row = CreditNoteRow()
        _copy_note(note, row)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # El número de la serie, o una segunda nota para el mismo comprobante.
            await self._session.rollback()
            raise InvoiceNumberTakenError from error
        return _credit_note(row)

    async def get(
        self, restaurant_id: int, credit_note_id: int, *, for_update: bool = False
    ) -> CreditNote | None:
        row = await self._row(restaurant_id, credit_note_id, for_update=for_update)
        return _credit_note(row) if row else None

    async def save(self, note: CreditNote) -> CreditNote:
        row = await self._row(note.restaurant_id, note.id or 0)
        if row is None:
            raise CreditNoteNotFoundError(note.id or 0)
        _copy_note(note, row)
        await self._session.flush()
        return _credit_note(row)

    async def next_number(self, restaurant_id: int, series: str) -> int:
        # El mismo turno que los comprobantes: la fila de datos fiscales del local.
        await locked_settings_row(self._session, restaurant_id)
        last = await self._session.scalar(
            select(func.max(CreditNoteRow.number)).where(
                CreditNoteRow.restaurant_id == restaurant_id, CreditNoteRow.series == series
            )
        )
        return int(last or 0) + 1

    async def _row(
        self, restaurant_id: int, credit_note_id: int, *, for_update: bool = False
    ) -> CreditNoteRow | None:
        statement = select(CreditNoteRow).where(
            CreditNoteRow.id == credit_note_id, CreditNoteRow.restaurant_id == restaurant_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(statement)).scalar_one_or_none()


# -- Pedidos pagados, leídos de las tablas de `orders` -------------------------

_orders = table(
    "orders",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("number", Integer),
    column("status", String),
    column("total", Numeric(10, 2)),
    column("discount_amount", Numeric(10, 2)),
    column("waiter_id", Integer),
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
    # Un plato anulado no se cobró: no va en el comprobante.
    column("voided_at", DateTime(timezone=True)),
)


def _description(name: str, modifiers: Sequence[object] | None) -> str:
    options = [str(m.get("option", "")) for m in modifiers or [] if isinstance(m, dict)]
    return f"{name} ({', '.join(options)})" if options else name


def _money(value: object) -> Decimal:
    """Un importe leído de la base, con sus dos decimales aunque venga vacío o en cero."""
    return Decimal(str(value if value is not None else 0)).quantize(CENT)


class SqlAlchemyPaidOrderDirectory:
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
                _order_items.c.voided_at.is_(None),
            )
            .order_by(_order_items.c.id)
        )
        return PaidOrder(
            id=int(order.id),
            number=int(order.number),
            waiter_id=int(order.waiter_id),
            total=Decimal(str(order.total)),
            # Un descuento de cero también es un Decimal falso: con `or 0` se
            # colaba el entero y el comprobante recién emitido decía "0", no "0.00".
            discount=_money(order.discount_amount),
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

    async def waiter_of(self, restaurant_id: int, order_id: int) -> int | None:
        waiter_id = (
            await self._session.execute(
                select(_orders.c.waiter_id).where(
                    _orders.c.id == order_id, _orders.c.restaurant_id == restaurant_id
                )
            )
        ).scalar_one_or_none()
        return None if waiter_id is None else int(waiter_id)
