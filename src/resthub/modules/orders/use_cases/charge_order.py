"""Cobrar: la cuenta entera de una vez, o por partes (dividida o con varios medios).

Exige `orders.charge`, que tienen los dos roles: los meseros hacen de cajeros.
Cada uno cobra los pedidos que tomó; el encargado, cualquiera. Todo cobro cae
en la caja abierta del local; sin caja abierta no se cobra.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Principal
from resthub.core.realtime import EventPublisher
from resthub.modules.orders.domain.exceptions import CashRegisterClosed
from resthub.modules.orders.domain.orders import ZERO, Order, OrderStatus, Payment, PaymentMethod
from resthub.modules.orders.ports.cash_register import CashRegister
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.use_cases.shared import (
    announce,
    ensure_owns_or_manages,
    find_visible_order,
)


@dataclass(frozen=True, slots=True)
class ChargeOrderCommand:
    actor: Principal
    order_id: int
    payment_method: PaymentMethod
    # Solo en efectivo: lo que entregó el cliente, para calcular el vuelto.
    amount_received: Decimal | None = None
    tip: Decimal = ZERO
    # Monto de una parte; sin él (y sin platos) se paga lo que falta.
    amount: Decimal | None = None
    # Los platos que paga esta persona, en una cuenta dividida por platos.
    item_ids: Sequence[int] = field(default_factory=tuple)
    # El saldo que vio quien cobra; si cambió, no se cobra.
    expected_balance: Decimal | None = None


class ChargeOrder:
    """Registra un pago del pedido servido: todo lo que falta o una parte."""

    def __init__(
        self,
        orders: OrderRepository,
        cash: CashRegister,
        activity: ActivityRecorder,
        events: EventPublisher,
    ) -> None:
        self._orders = orders
        self._cash = cash
        self._activity = activity
        self._events = events

    async def __call__(self, command: ChargeOrderCommand) -> Order:
        actor = command.actor
        order = await find_visible_order(self._orders, actor, command.order_id, for_update=True)
        ensure_owns_or_manages(order, actor, "cobrarlo")
        # La caja también queda tomada: un cierre que empieza ahora espera a
        # que este pago se guarde, así el arqueo no lo deja afuera.
        session = await self._cash.current(actor.restaurant_id, for_update=True)
        if session is None:
            raise CashRegisterClosed()

        payment = order.add_payment(
            command.payment_method,
            datetime.now(UTC),
            received_by=actor.user_id,
            amount=command.amount,
            item_ids=tuple(command.item_ids),
            amount_received=command.amount_received,
            tip=command.tip,
            cash_session_id=session.id,
            expected_balance=command.expected_balance,
        )
        saved = await self._orders.save(order)
        await self._activity.record(
            saved.restaurant_id, actor.user_id, *_activity_entry(saved, payment)
        )
        announce(self._events, saved)
        return saved


def _activity_entry(order: Order, payment: Payment) -> tuple[ActivityKind, str]:
    tip = f" + S/ {payment.tip} de propina" if payment.tip > 0 else ""
    if order.status is OrderStatus.PAID and len(order.payments) == 1:
        return (
            ActivityKind.ORDER_CHARGED,
            f"Pedido #{order.number}: S/ {order.total} en {payment.method.label}{tip}",
        )
    closing = " (cuenta cerrada)" if order.status is OrderStatus.PAID else ""
    return (
        ActivityKind.PAYMENT_RECEIVED,
        f"Pedido #{order.number}: S/ {payment.amount} en {payment.method.label}{tip}; "
        f"faltan S/ {order.balance}{closing}",
    )
