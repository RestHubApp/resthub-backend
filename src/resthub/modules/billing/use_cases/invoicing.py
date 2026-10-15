"""Emitir boletas y facturas, reenviarlas y configurar los datos fiscales.

Emitir exige `billing.issue` (los meseros cobran, así que también emiten), y
sin `orders.read_all` solo se emite o se ve el comprobante de un pedido propio.
Configurar, listar y reenviar exigen `billing.manage` (el encargado).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.identity import Principal
from resthub.core.pagination import Page
from resthub.core.permissions import Permission
from resthub.modules.billing.domain.exceptions import (
    InvalidBillingSettingsError,
    InvalidCertificateError,
    InvoiceDocumentUnavailableError,
    InvoiceNotFoundError,
    OrderAlreadyInvoicedError,
    PaidOrderNotFoundError,
)
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Customer,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    build_lines,
    validate_customer,
)
from resthub.modules.billing.ports.billing_ports import (
    BillingSettingsRepository,
    CertificateReader,
    ElectronicInvoiceGateway,
    InvoiceQuery,
    InvoiceRepository,
    PaidOrderDirectory,
)
from resthub.modules.billing.ports.delivery_ports import DocumentFile, InvoiceArchiveHook


def can_see_paid_order(actor: Principal, waiter_id: int | None) -> bool:
    """La regla de `orders` para un pedido ya cobrado: el de quien lo tomó.

    Sin `orders.read_all` (el mesero) solo se ve lo propio; lo cobrado por
    otro responde 404, igual que `GET /orders/{id}`. Un pedido con comprobante
    ya está pagado, así que la excepción de los pedidos activos no aplica.
    """
    if waiter_id is None:
        return False
    return Permission.ORDERS_READ_ALL in actor.permissions or waiter_id == actor.user_id


@dataclass(frozen=True, slots=True)
class IssueInvoiceCommand:
    actor: Principal
    order_id: int
    kind: InvoiceKind
    customer: Customer


class IssueInvoice:
    """Genera el comprobante de un pedido pagado y lo envía al proveedor.

    El comprobante se guarda siempre, aunque el envío falle: queda pendiente y
    el encargado lo reenvía. Sin proveedor configurado queda «sin enviar».
    """

    def __init__(
        self,
        invoices: InvoiceRepository,
        settings: BillingSettingsRepository,
        orders: PaidOrderDirectory,
        invoicer: ElectronicInvoiceGateway,
        activity: ActivityRecorder,
        *,
        clock: Clock,
        archive: InvoiceArchiveHook | None = None,
    ) -> None:
        self._invoices = invoices
        self._settings = settings
        self._orders = orders
        self._invoicer = invoicer
        self._activity = activity
        self._clock = clock
        self._archive = archive

    async def __call__(self, command: IssueInvoiceCommand) -> Invoice:
        restaurant_id = command.actor.restaurant_id
        order = await self._orders.get(restaurant_id, command.order_id)
        if order is None or not can_see_paid_order(command.actor, order.waiter_id):
            raise PaidOrderNotFoundError(command.order_id)
        existing = await self._invoices.for_order(restaurant_id, order.id)
        if existing is not None:
            raise OrderAlreadyInvoicedError(existing.code)
        config = await self._settings.get(restaurant_id)
        customer = validate_customer(command.kind, command.customer, order.total)
        series = config.series_for(command.kind)
        invoice = await self._invoices.add(
            Invoice(
                restaurant_id=restaurant_id,
                order_id=order.id,
                kind=command.kind,
                series=series,
                number=await self._invoices.next_number(restaurant_id, series),
                customer=customer,
                lines=build_lines(order.items),
                total=order.total,
                discount=order.discount,
                igv_rate=config.igv_rate,
                issued_by=command.actor.user_id,
                issued_at=self._clock.now(),
            )
        )
        sent = await _send(self._invoicer, self._invoices, config, invoice, self._archive)
        await self._activity.record(
            restaurant_id,
            command.actor.user_id,
            ActivityKind.INVOICE_ISSUED,
            f"{sent.kind.label} {sent.code} del pedido #{order.number} (S/ {sent.total}): "
            f"{sent.status.label}",
        )
        return sent


async def _send(
    invoicer: ElectronicInvoiceGateway,
    invoices: InvoiceRepository,
    config: BillingSettings,
    invoice: Invoice,
    archive: InvoiceArchiveHook | None = None,
) -> Invoice:
    if not config.is_ready:
        invoice.record_result(
            InvoiceStatus.SIMULATED,
            "Faltan el RUC, el usuario SOL o el certificado: se reenvía al cargarlos.",
            {},
        )
    else:
        result = await invoicer.send(config, invoice, invoice.kind)
        invoice.record_result(
            result.status,
            result.message,
            result.raw or {},
            signed_xml=result.signed_xml,
            cdr_zip=result.cdr_zip,
            document_payload=result.document_payload,
        )
    # Aceptado y con documentos: se respaldan fuera de la petición, para no frenar el cobro.
    queue_archive = (
        archive is not None and invoice.has_documents_to_archive and archive.is_enabled()
    )
    if queue_archive:
        invoice.mark_archive_pending()
    saved = await invoices.save(invoice)
    if queue_archive and archive is not None and saved.id is not None:
        archive.schedule(saved.restaurant_id, saved.id)
    return saved


class ResendInvoice:
    """Vuelve a enviar un comprobante pendiente, rechazado o sin enviar."""

    def __init__(
        self,
        invoices: InvoiceRepository,
        settings: BillingSettingsRepository,
        invoicer: ElectronicInvoiceGateway,
        archive: InvoiceArchiveHook | None = None,
    ) -> None:
        self._invoices = invoices
        self._settings = settings
        self._invoicer = invoicer
        self._archive = archive

    async def __call__(self, restaurant_id: int, invoice_id: int) -> Invoice:
        # Tomado: dos reenvíos a la vez mandarían el mismo número al proveedor,
        # y el rechazo por duplicado del segundo pisaría la aceptación del primero.
        invoice = await find_invoice(self._invoices, restaurant_id, invoice_id, for_update=True)
        invoice.ensure_resendable()
        return await _send(
            self._invoicer,
            self._invoices,
            await self._settings.get(restaurant_id),
            invoice,
            self._archive,
        )


async def find_invoice(
    invoices: InvoiceRepository, restaurant_id: int, invoice_id: int, *, for_update: bool = False
) -> Invoice:
    invoice = await invoices.get(restaurant_id, invoice_id, for_update=for_update)
    if invoice is None:
        raise InvoiceNotFoundError(invoice_id)
    return invoice


class ReadInvoice:
    """Un comprobante, por su número o por su pedido, si quien pide ve ese pedido."""

    def __init__(self, invoices: InvoiceRepository, orders: PaidOrderDirectory) -> None:
        self._invoices = invoices
        self._orders = orders

    async def by_id(self, actor: Principal, invoice_id: int) -> Invoice:
        invoice = await find_invoice(self._invoices, actor.restaurant_id, invoice_id)
        if not await self._visible(actor, invoice.order_id):
            raise InvoiceNotFoundError(invoice_id)
        return invoice

    async def for_order(self, actor: Principal, order_id: int) -> Invoice | None:
        """`None` si el pedido no tiene comprobante o no le toca verlo: no se distingue."""
        if not await self._visible(actor, order_id):
            return None
        return await self._invoices.for_order(actor.restaurant_id, order_id)

    async def _visible(self, actor: Principal, order_id: int) -> bool:
        if Permission.ORDERS_READ_ALL in actor.permissions:
            return True
        return can_see_paid_order(
            actor, await self._orders.waiter_of(actor.restaurant_id, order_id)
        )


class ListInvoices:
    def __init__(self, invoices: InvoiceRepository) -> None:
        self._invoices = invoices

    async def __call__(self, query: InvoiceQuery) -> Page[Invoice]:
        return await self._invoices.search(query)


RETYPE_ON_RUC_CHANGE = "Si cambias el RUC, vuelve a escribir el usuario y la clave SOL de ese RUC."


@dataclass(frozen=True, slots=True)
class SettingsChange:
    ruc: str
    legal_name: str
    address: str
    igv_rate: Decimal
    sales_receipt_series: str
    tax_invoice_series: str
    # `None` deja el usuario y la clave SOL como estaban (solo si el RUC no cambia); vacíos
    # los borran. Se cambian juntos.
    sol_user: str | None = None
    sol_password: str | None = None


async def _register(
    invoicer: ElectronicInvoiceGateway, before: BillingSettings, after: BillingSettings
) -> None:
    """Deja el servicio de facturación al día con las credenciales del local.

    Se registra cuando cambió algo de lo que usa para firmar; si el RUC cambió, el
    anterior se olvida. Va antes de guardar: lo guardado es lo registrado.
    """
    changed = (
        before.ruc != after.ruc
        or before.sol_user != after.sol_user
        or before.sol_password != after.sol_password
        or before.certificate_pem != after.certificate_pem
    )
    if changed and after.can_register:
        await invoicer.register(after)
    if before.ruc and before.ruc != after.ruc and before.can_register:
        await invoicer.unregister(before.ruc)


class UpdateBillingSettings:
    def __init__(
        self,
        settings: BillingSettingsRepository,
        activity: ActivityRecorder,
        invoicer: ElectronicInvoiceGateway,
    ) -> None:
        self._settings = settings
        self._activity = activity
        self._invoicer = invoicer

    async def __call__(
        self, restaurant_id: int, actor_id: int, change: SettingsChange
    ) -> BillingSettings:
        current = await self._settings.get(restaurant_id)
        ruc_changed = change.ruc.strip() != current.ruc
        if (change.sol_user is None) != (change.sol_password is None):
            message = "El usuario y la clave SOL se cambian juntos."
            raise InvalidBillingSettingsError(message)
        if change.sol_user is None or change.sol_password is None:
            # Las credenciales son de un RUC: con otro RUC no sirven.
            if ruc_changed and current.has_sol_credentials:
                raise InvalidBillingSettingsError(RETYPE_ON_RUC_CHANGE)
            sol_user, sol_password = current.sol_user, current.sol_password
        else:
            sol_user, sol_password = change.sol_user, change.sol_password
        updated = BillingSettings(
            restaurant_id=restaurant_id,
            ruc=change.ruc,
            legal_name=change.legal_name,
            address=change.address,
            igv_rate=change.igv_rate,
            sales_receipt_series=change.sales_receipt_series,
            tax_invoice_series=change.tax_invoice_series,
            sol_user=sol_user,
            sol_password=sol_password,
            certificate_pem=current.certificate_pem,
            certificate_expires_at=current.certificate_expires_at,
            timezone=current.timezone,
        )
        await _register(self._invoicer, current, updated)
        saved = await self._settings.save(updated)
        detail = (
            f"RUC {saved.ruc or '—'}, IGV {saved.igv_rate} %, "
            f"series {saved.sales_receipt_series} y {saved.tax_invoice_series}"
        )
        # Nunca el usuario ni la clave: solo que cambiaron.
        if change.sol_user:
            detail += "; credenciales SOL reemplazadas"
        elif change.sol_user is not None and current.has_sol_credentials:
            detail += "; credenciales SOL borradas"
        await self._activity.record(
            restaurant_id, actor_id, ActivityKind.BILLING_SETTINGS_UPDATED, detail
        )
        return saved


@dataclass(frozen=True, slots=True)
class UploadCertificateCommand:
    restaurant_id: int
    actor_id: int
    content: bytes
    password: str


class UploadCertificate:
    """Reemplaza el certificado digital del local, si abre y no está vencido."""

    def __init__(
        self,
        settings: BillingSettingsRepository,
        reader: CertificateReader,
        invoicer: ElectronicInvoiceGateway,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._reader = reader
        self._invoicer = invoicer
        self._activity = activity
        self._clock = clock

    async def __call__(self, command: UploadCertificateCommand) -> BillingSettings:
        parsed = self._reader.read(command.content, command.password)
        if parsed.expires_at is not None and parsed.expires_at <= self._clock.now():
            day = parsed.expires_at.date().isoformat()
            message = f"El certificado venció el {day}: sube uno vigente."
            raise InvalidCertificateError(message)
        current = await self._settings.get(command.restaurant_id)
        updated = replace(
            current, certificate_pem=parsed.pem, certificate_expires_at=parsed.expires_at
        )
        await _register(self._invoicer, current, updated)
        saved = await self._settings.save(updated)
        expires = parsed.expires_at.date().isoformat() if parsed.expires_at else "sin fecha"
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.BILLING_SETTINGS_UPDATED,
            f"Certificado digital reemplazado ({parsed.subject or 'sin nombre'}, vence {expires})",
        )
        return saved


class DocumentFormat(StrEnum):
    PDF = "pdf"
    XML = "xml"
    CDR = "cdr"


async def invoice_document(
    invoice: Invoice, document_format: DocumentFormat, invoicer: ElectronicInvoiceGateway
) -> DocumentFile:
    """El PDF (armado al momento), el XML firmado o la constancia de SUNAT de un comprobante."""
    if document_format is DocumentFormat.XML:
        if not invoice.has_xml:
            message = f"El comprobante {invoice.code} todavía no tiene XML firmado."
            raise InvoiceDocumentUnavailableError(message)
        return DocumentFile(content=invoice.signed_xml.encode(), media_type="application/xml")
    if document_format is DocumentFormat.CDR:
        if not invoice.has_cdr:
            message = f"El comprobante {invoice.code} todavía no tiene constancia de SUNAT."
            raise InvoiceDocumentUnavailableError(message)
        try:
            content = base64.b64decode(invoice.cdr_zip, validate=True)
        except (binascii.Error, ValueError):
            message = f"La constancia de {invoice.code} está dañada; reenvía el comprobante."
            raise InvoiceDocumentUnavailableError(message) from None
        return DocumentFile(content=content, media_type="application/zip")
    if not invoice.has_pdf:
        message = f"El comprobante {invoice.code} todavía no tiene PDF."
        raise InvoiceDocumentUnavailableError(message)
    return DocumentFile(content=await invoicer.render_pdf(invoice), media_type="application/pdf")


def document_filename(invoice: Invoice, document_format: DocumentFormat) -> str:
    """«B001-15.pdf», «B001-15.xml» y «R-B001-15.zip», como los nombra SUNAT."""
    if document_format is DocumentFormat.CDR:
        return f"R-{invoice.code}.zip"
    return f"{invoice.code}.{document_format.value}"


class ReadInvoiceDocument:
    """Descargar un documento exige poder ver el comprobante."""

    def __init__(
        self,
        invoices: InvoiceRepository,
        orders: PaidOrderDirectory,
        invoicer: ElectronicInvoiceGateway,
    ) -> None:
        self._reader = ReadInvoice(invoices, orders)
        self._invoicer = invoicer

    async def __call__(
        self, actor: Principal, invoice_id: int, document_format: DocumentFormat
    ) -> tuple[DocumentFile, str]:
        invoice = await self._reader.by_id(actor, invoice_id)
        document = await invoice_document(invoice, document_format, self._invoicer)
        return document, document_filename(invoice, document_format)
