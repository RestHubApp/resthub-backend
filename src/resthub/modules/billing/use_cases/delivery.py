"""Enviar al cliente, por correo, un comprobante ya emitido.

Exige `billing.issue` y la misma visibilidad que leerlo: sin `orders.read_all`
solo el comprobante de un pedido propio. El mensaje lleva el enlace al PDF que
dio el proveedor. En la bitácora queda el correo enmascarado, nunca completo.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.identity import Principal
from resthub.modules.billing.domain.exceptions import (
    EmailRateLimitedError,
    EmailSendingDisabledError,
    InvalidInvoiceError,
)
from resthub.modules.billing.domain.invoices import Invoice, mask_email
from resthub.modules.billing.domain.send_limit import SendLimit
from resthub.modules.billing.ports.billing_ports import (
    BillingSettingsRepository,
    InvoiceRepository,
    PaidOrderDirectory,
)
from resthub.modules.billing.ports.delivery_ports import EmailGateway, OutgoingEmail
from resthub.modules.billing.use_cases.invoicing import ReadInvoice


@dataclass(frozen=True, slots=True)
class SendInvoiceEmailCommand:
    actor: Principal
    invoice_id: int
    to_address: str
    to_name: str = ""


def compose_email(
    invoice: Invoice, business_name: str, to_address: str, to_name: str
) -> OutgoingEmail:
    """Un texto sobrio con el total y el enlace al PDF."""
    name = business_name or "el restaurante"
    greeting = f"Hola {to_name}," if to_name else "Hola,"
    kind = invoice.kind.label.lower()
    subject = f"{invoice.kind.label} {invoice.code} de {name}"
    text = "\n".join(
        [
            greeting,
            "",
            (
                f"Te enviamos el enlace de tu {kind} {invoice.code} por S/ {invoice.total}, "
                f"emitida por {name}."
            ),
            "",
            f"Descárgala aquí: {invoice.pdf_url}",
            "",
            "Gracias por tu preferencia.",
        ]
    )
    link = html.escape(invoice.pdf_url, quote=True)
    body = (
        f"<p>{html.escape(greeting)}</p>"
        f"<p>Te enviamos el enlace de tu {html.escape(kind)} "
        f"<strong>{html.escape(invoice.code)}</strong> por S/ {invoice.total}, "
        f"emitida por {html.escape(name)}.</p>"
        f'<p><a href="{link}">Descargar el comprobante (PDF)</a></p>'
        "<p>Gracias por tu preferencia.</p>"
    )
    return OutgoingEmail(
        to_address=to_address, to_name=to_name, subject=subject[:200], html=body, text=text
    )


class SendInvoiceEmail:
    def __init__(
        self,
        *,
        invoices: InvoiceRepository,
        orders: PaidOrderDirectory,
        settings: BillingSettingsRepository,
        gateway: EmailGateway,
        activity: ActivityRecorder,
        limit: SendLimit,
        clock: Clock,
    ) -> None:
        self._reader = ReadInvoice(invoices, orders)
        self._settings = settings
        self._gateway = gateway
        self._activity = activity
        self._limit = limit
        self._clock = clock

    async def __call__(self, command: SendInvoiceEmailCommand) -> Invoice:
        if not self._gateway.is_enabled():
            raise EmailSendingDisabledError
        actor = command.actor
        invoice = await self._reader.by_id(actor, command.invoice_id)
        if not invoice.pdf_url:
            message = f"El comprobante {invoice.code} todavía no tiene PDF para enviar."
            raise InvalidInvoiceError(message)
        if not self._limit.try_take(actor.user_id, self._clock.now()):
            raise EmailRateLimitedError(self._limit.max_per_window)
        config = await self._settings.get(actor.restaurant_id)
        await self._gateway.send(
            compose_email(invoice, config.legal_name, command.to_address, command.to_name.strip())
        )
        await self._activity.record(
            actor.restaurant_id,
            actor.user_id,
            ActivityKind.INVOICE_EMAILED,
            f"{invoice.kind.label} {invoice.code} enviada a {mask_email(command.to_address)}",
        )
        return invoice
