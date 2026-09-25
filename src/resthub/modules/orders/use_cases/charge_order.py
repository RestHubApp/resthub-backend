from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Principal
from resthub.core.realtime import EventPublisher
from resthub.modules.orders.domain.orders import Order, PaymentMethod
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.use_cases.shared import announce, find_visible_order


@dataclass(frozen=True, slots=True)
class ChargeOrderCommand:
    actor: Principal
    order_id: int
    payment_method: PaymentMethod
    # Solo en efectivo: lo que entregó el cliente, para calcular el vuelto.
    amount_received: Decimal | None = None


class ChargeOrder:
    """Cobra un pedido servido. Exige `orders.charge`: la caja es del encargado."""

    def __init__(
        self, orders: OrderRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._orders = orders
        self._activity = activity
        self._events = events

    async def __call__(self, command: ChargeOrderCommand) -> Order:
        order = await find_visible_order(self._orders, command.actor, command.order_id)
        order.charge(
            command.payment_method, datetime.now(UTC), amount_received=command.amount_received
        )
        saved = await self._orders.save(order)
        await self._activity.record(
            saved.restaurant_id,
            command.actor.user_id,
            ActivityKind.ORDER_CHARGED,
            f"Pedido #{saved.number}: S/ {saved.total} en {command.payment_method.label}",
        )
        announce(self._events, saved)
        return saved
