"""Anular un cobro mal hecho: el medio equivocado, un monto de más (RFS-25).

Exige `cash.manage`, como la caja, y un motivo. Solo mientras el turno de caja
en que entró siga abierto: el arqueo de uno cerrado no cambia. Con el
comprobante emitido, primero se anula el comprobante (nota de crédito).
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.identity import Principal
from resthub.core.realtime import EventPublisher
from resthub.modules.orders.domain.exceptions import (
    OrderAlreadyInvoicedError,
    PaymentNotFoundError,
    PaymentNotVoidableError,
)
from resthub.modules.orders.domain.orders import Order
from resthub.modules.orders.ports.cash_register import CashRegisterRepository
from resthub.modules.orders.ports.invoice_status import InvoiceStatusReader
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.use_cases.shared import announce, find_visible_order


@dataclass(frozen=True, slots=True)
class VoidPaymentCommand:
    actor: Principal
    order_id: int
    payment_id: int
    reason: str


class VoidPayment:
    def __init__(
        self,
        orders: OrderRepository,
        cash: CashRegisterRepository,
        invoices: InvoiceStatusReader,
        activity: ActivityRecorder,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._orders = orders
        self._cash = cash
        self._invoices = invoices
        self._activity = activity
        self._events = events
        self._clock = clock

    async def __call__(self, command: VoidPaymentCommand) -> Order:
        actor = command.actor
        order = await find_visible_order(self._orders, actor, command.order_id, for_update=True)
        payment = next((paid for paid in order.payments if paid.id == command.payment_id), None)
        if payment is None:
            raise PaymentNotFoundError(command.payment_id)
        if await self._invoices.has_live_invoice(order.restaurant_id, order.id or 0):
            raise OrderAlreadyInvoicedError
        # La caja queda tomada, como al cobrar: un cierre que empieza ahora
        # espera, y el arqueo no cuenta un cobro que se está anulando.
        session = await self._cash.current(actor.restaurant_id, for_update=True)
        if session is None or payment.cash_session_id != session.id:
            raise PaymentNotVoidableError
        voided = order.void_payment(
            command.payment_id, command.reason, actor.user_id, self._clock.now()
        )
        saved = await self._orders.save(order)
        tip = f" + S/ {voided.tip} de propina" if voided.tip > 0 else ""
        await self._activity.record(
            saved.restaurant_id,
            actor.user_id,
            ActivityKind.PAYMENT_VOIDED,
            (
                f"Pedido #{saved.number}: S/ {voided.amount} en {voided.method.label}{tip}: "
                f"{voided.void_reason}"
            ),
        )
        announce(self._events, saved)
        return saved
