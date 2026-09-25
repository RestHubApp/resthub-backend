"""Casos de uso del encargado sobre la cocina: marcar listo y cancelar.

Exigen `orders.manage`. El mesero no los tiene: que un pedido está listo lo
dice quien ve la cocina, y cancelar es una decisión con consecuencias en caja.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Principal
from resthub.core.realtime import EventPublisher
from resthub.modules.orders.domain.orders import Order
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.use_cases.shared import announce, find_visible_order


class MarkReady:
    def __init__(self, orders: OrderRepository, events: EventPublisher) -> None:
        self._orders = orders
        self._events = events

    async def __call__(self, actor: Principal, order_id: int) -> Order:
        order = await find_visible_order(self._orders, actor, order_id)
        order.mark_ready(datetime.now(UTC))
        saved = await self._orders.save(order)
        announce(self._events, saved)
        return saved


@dataclass(frozen=True, slots=True)
class CancelOrderCommand:
    actor: Principal
    order_id: int
    reason: str


class CancelOrder:
    """Cancela un pedido activo con su motivo.

    Lo ya servido no se devuelve al inventario: la comida se preparó y se gastó
    aunque no se haya cobrado. Justamente por eso queda en la bitácora.
    """

    def __init__(
        self, orders: OrderRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._orders = orders
        self._activity = activity
        self._events = events

    async def __call__(self, command: CancelOrderCommand) -> Order:
        order = await find_visible_order(self._orders, command.actor, command.order_id)
        order.cancel(command.reason, datetime.now(UTC))
        saved = await self._orders.save(order)
        await self._activity.record(
            saved.restaurant_id,
            command.actor.user_id,
            ActivityKind.ORDER_CANCELLED,
            f"Pedido #{saved.number} (S/ {saved.total}): {saved.cancel_reason}",
        )
        announce(self._events, saved)
        return saved
