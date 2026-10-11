"""Emitir boletas y facturas, reenviarlas y configurar los datos fiscales.

Emitir exige `billing.issue` (los meseros cobran, así que también emiten), y
sin `orders.read_all` solo se emite o se ve el comprobante de un pedido propio.
Configurar, listar y reenviar exigen `billing.manage` (el encargado).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Principal
from resthub.core.pagination import Page
from resthub.core.permissions import Permission
from resthub.modules.billing.domain.exceptions import (
    InvalidBillingSettings,
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
    provider_host,
    same_provider_destination,
    validate_customer,
    validate_provider_url,
)
from resthub.modules.billing.ports.billing_ports import (
    BillingSettingsRepository,
    ElectronicInvoicer,
    InvoiceQuery,
    InvoiceRepository,
    PaidOrderDirectory,
)


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
        if order is None or not can_see_paid_order(command.actor, order.waiter_id):
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
        # Tomado: dos reenvíos a la vez mandarían el mismo número al proveedor,
        # y el rechazo por duplicado del segundo pisaría la aceptación del primero.
        invoice = await find_invoice(self._invoices, restaurant_id, invoice_id, for_update=True)
        invoice.ensure_resendable()
        return await _send(
            self._invoicer, self._invoices, await self._settings.get(restaurant_id), invoice
        )


async def find_invoice(
    invoices: InvoiceRepository, restaurant_id: int, invoice_id: int, *, for_update: bool = False
) -> Invoice:
    invoice = await invoices.get(restaurant_id, invoice_id, for_update=for_update)
    if invoice is None:
        raise InvoiceNotFound(invoice_id)
    return invoice


class ReadInvoice:
    """Un comprobante, por su número o por su pedido, si quien pide ve ese pedido."""

    def __init__(self, invoices: InvoiceRepository, orders: PaidOrderDirectory) -> None:
        self._invoices = invoices
        self._orders = orders

    async def by_id(self, actor: Principal, invoice_id: int) -> Invoice:
        invoice = await find_invoice(self._invoices, actor.restaurant_id, invoice_id)
        if not await self._visible(actor, invoice.order_id):
            raise InvoiceNotFound(invoice_id)
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


RETYPE_ON_URL_CHANGE = "Si cambias la ruta del proveedor, vuelve a escribir su token."


@dataclass(frozen=True, slots=True)
class SettingsChange:
    ruc: str
    legal_name: str
    address: str
    igv_rate: Decimal
    boleta_series: str
    factura_series: str
    provider_url: str
    # `None` deja el token como estaba (solo si la URL no cambia); vacío lo borra.
    provider_token: str | None = None


class UpdateBillingSettings:
    def __init__(self, settings: BillingSettingsRepository, activity: ActivityRecorder) -> None:
        self._settings = settings
        self._activity = activity

    async def __call__(
        self, restaurant_id: int, actor_id: int, change: SettingsChange
    ) -> BillingSettings:
        current = await self._settings.get(restaurant_id)
        # El servidor le manda los comprobantes y el token a esa URL: no
        # puede apuntar a una dirección interna ni ir sin cifrar.
        provider_url = validate_provider_url(change.provider_url)
        url_changed = not same_provider_destination(current.provider_url, provider_url)
        if change.provider_token is None:
            # El token viaja a la URL: si la URL cambia, quien la cambia tiene
            # que conocer el token. Si no, cualquiera con `billing.manage` se lo
            # llevaría a un servidor suyo. Quitar la URL no manda nada a nadie.
            if url_changed and provider_url and current.provider_token:
                raise InvalidBillingSettings(RETYPE_ON_URL_CHANGE)
            provider_token = current.provider_token
        else:
            provider_token = change.provider_token
        updated = BillingSettings(
            restaurant_id=restaurant_id,
            ruc=change.ruc,
            legal_name=change.legal_name,
            address=change.address,
            igv_rate=change.igv_rate,
            boleta_series=change.boleta_series,
            factura_series=change.factura_series,
            provider_url=provider_url,
            provider_token=provider_token,
            timezone=current.timezone,
        )
        saved = await self._settings.save(updated)
        detail = (
            f"RUC {saved.ruc or '—'}, IGV {saved.igv_rate} %, series {saved.boleta_series} y "
            f"{saved.factura_series}"
        )
        # Nunca el token ni la ruta (puede llevar la clave de la cuenta): el servidor basta.
        if change.provider_token:
            detail += "; token del proveedor reemplazado"
        elif change.provider_token is not None and current.provider_token:
            detail += "; token del proveedor borrado"
        if url_changed:
            host = provider_host(saved.provider_url)
            detail += f"; proveedor en {host}" if host else "; sin proveedor"
        await self._activity.record(
            restaurant_id, actor_id, ActivityKind.BILLING_SETTINGS_UPDATED, detail
        )
        return saved
