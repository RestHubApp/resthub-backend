"""Emitir boletas y facturas, reenviarlas y configurar los datos fiscales.

Emitir exige `billing.issue` (los meseros cobran, así que también emiten).
Configurar, listar y reenviar exigen `billing.manage` (el encargado).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Principal
from resthub.core.pagination import Page
from resthub.modules.billing.domain.exceptions import (
    InvoiceNotFound,
    OrderAlreadyInvoiced,
    PaidOrderNotFound,
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
    ElectronicInvoicer,
    InvoiceQuery,
    InvoiceRepository,
    PaidOrderDirectory,
)


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
        invoicer: ElectronicInvoicer,
        activity: ActivityRecorder,
    ) -> None:
        self._invoices = invoices
        self._settings = settings
        self._orders = orders
        self._invoicer = invoicer
        self._activity = activity

    async def __call__(self, command: IssueInvoiceCommand) -> Invoice:
        restaurant_id = command.actor.restaurant_id
        order = await self._orders.get(restaurant_id, command.order_id)
        if order is None:
            raise PaidOrderNotFound(command.order_id)
        existing = await self._invoices.for_order(restaurant_id, order.id)
        if existing is not None:
            raise OrderAlreadyInvoiced(existing.code)
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
                issued_at=datetime.now(UTC),
            )
        )
        sent = await _send(self._invoicer, self._invoices, config, invoice)
        await self._activity.record(
            restaurant_id,
            command.actor.user_id,
            ActivityKind.INVOICE_ISSUED,
            f"{sent.kind.label} {sent.code} del pedido #{order.number} (S/ {sent.total}): "
            f"{sent.status.label}",
        )
        return sent


async def _send(
    invoicer: ElectronicInvoicer,
    invoices: InvoiceRepository,
    config: BillingSettings,
    invoice: Invoice,
) -> Invoice:
    if not config.is_ready:
        invoice.record_result(
            InvoiceStatus.SIMULATED,
            "Falta configurar el RUC y el proveedor: se reenvía al cargarlos.",
            "",
            {},
        )
    else:
        result = await invoicer.send(config, invoice, invoice.kind)
        invoice.record_result(result.status, result.message, result.pdf_url, result.raw or {})
    return await invoices.save(invoice)


class ResendInvoice:
    """Vuelve a enviar un comprobante pendiente, rechazado o sin enviar."""

    def __init__(
        self,
        invoices: InvoiceRepository,
        settings: BillingSettingsRepository,
        invoicer: ElectronicInvoicer,
    ) -> None:
        self._invoices = invoices
        self._settings = settings
        self._invoicer = invoicer

    async def __call__(self, restaurant_id: int, invoice_id: int) -> Invoice:
        invoice = await find_invoice(self._invoices, restaurant_id, invoice_id)
        invoice.ensure_resendable()
        return await _send(
            self._invoicer, self._invoices, await self._settings.get(restaurant_id), invoice
        )


async def find_invoice(invoices: InvoiceRepository, restaurant_id: int, invoice_id: int) -> Invoice:
    invoice = await invoices.get(restaurant_id, invoice_id)
    if invoice is None:
        raise InvoiceNotFound(invoice_id)
    return invoice


class ListInvoices:
    def __init__(self, invoices: InvoiceRepository) -> None:
        self._invoices = invoices

    async def __call__(self, query: InvoiceQuery) -> Page[Invoice]:
        return await self._invoices.search(query)


@dataclass(frozen=True, slots=True)
class SettingsChange:
    ruc: str
    legal_name: str
    address: str
    igv_rate: Decimal
    boleta_series: str
    factura_series: str
    provider_url: str
    # `None` deja el token como estaba; vacío lo borra.
    provider_token: str | None = None


class UpdateBillingSettings:
    def __init__(self, settings: BillingSettingsRepository, activity: ActivityRecorder) -> None:
        self._settings = settings
        self._activity = activity

    async def __call__(
        self, restaurant_id: int, actor_id: int, change: SettingsChange
    ) -> BillingSettings:
        current = await self._settings.get(restaurant_id)
        updated = BillingSettings(
            restaurant_id=restaurant_id,
            ruc=change.ruc,
            legal_name=change.legal_name,
            address=change.address,
            igv_rate=change.igv_rate,
            boleta_series=change.boleta_series,
            factura_series=change.factura_series,
            provider_url=change.provider_url,
            provider_token=(
                current.provider_token if change.provider_token is None else change.provider_token
            ),
        )
        saved = await self._settings.save(updated)
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.BILLING_SETTINGS_UPDATED,
            f"RUC {saved.ruc or '—'}, IGV {saved.igv_rate} %, series {saved.boleta_series} y "
            f"{saved.factura_series}",
        )
        return saved
