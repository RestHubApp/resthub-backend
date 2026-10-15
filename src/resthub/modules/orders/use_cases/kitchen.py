"""Casos de uso del encargado sobre la cocina: marcar listo, anular un plato y cancelar.

Exigen `orders.manage`. El mesero no los tiene: que un pedido está listo lo
dice quien ve la cocina, y anular o cancelar es una decisión con consecuencias
en caja.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.identity import Principal
from resthub.core.realtime import EventPublisher
from resthub.modules.orders.domain.orders import Order
from resthub.modules.orders.ports.consumed_items_reader import ConsumedItemsReader
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.use_cases.shared import announce, find_visible_order


class MarkReady:
    def __init__(self, orders: OrderRepository, events: EventPublisher, clock: Clock) -> None:
        self._orders = orders
        self._events = events
        self._clock = clock

    async def __call__(self, actor: Principal, order_id: int) -> Order:
        order = await find_visible_order(self._orders, actor, order_id, for_update=True)
        order.mark_ready(self._clock.now())
        saved = await self._orders.save(order)
        announce(self._events, saved)
        return saved


# Lo que ve el encargado en la bitácora cuando el plato ya gastó insumos.
CONSUMED_VOID_NOTE = "Plato anulado tras consumo"


@dataclass(frozen=True, slots=True)
class VoidItemCommand:
    actor: Principal
    order_id: int
    item_id: int
    reason: str


class VoidItem:
    """Anula un plato que ya salió a cocina y antes del primer pago.

    Deja de cobrarse y no descuenta insumos si todavía no se sirvió. Si ya se
    sirvió, lo gastado no vuelve al inventario (la comida se preparó), y la
    bitácora lo marca para que el encargado lo vea.
    """

    def __init__(
        self,
        orders: OrderRepository,
        consumption: ConsumedItemsReader,
        activity: ActivityRecorder,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._orders = orders
        self._consumption = consumption
        self._activity = activity
        self._events = events
        self._clock = clock

    async def __call__(self, command: VoidItemCommand) -> Order:
        order = await find_visible_order(
            self._orders, command.actor, command.order_id, for_update=True
        )
        item = order.void_item(command.item_id, command.reason, self._clock.now())
        saved = await self._orders.save(order)
        consumed = await self._consumption.consumed(saved.restaurant_id, {command.item_id})
        detail = f"Pedido #{saved.number}: {item.name} ×{item.quantity}: {item.void_reason}"  # noqa: RUF001 - signo de multiplicar en un texto para personas
        if consumed:
            detail = f"{detail} ({CONSUMED_VOID_NOTE})"
        await self._activity.record(
            saved.restaurant_id, command.actor.user_id, ActivityKind.ORDER_ITEM_VOIDED, detail
        )
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
        self,
        orders: OrderRepository,
        activity: ActivityRecorder,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._orders = orders
        self._activity = activity
        self._events = events
        self._clock = clock

    async def __call__(self, command: CancelOrderCommand) -> Order:
        order = await find_visible_order(
            self._orders, command.actor, command.order_id, for_update=True
        )
        order.cancel(command.reason, self._clock.now())
        saved = await self._orders.save(order)
        await self._activity.record(
            saved.restaurant_id,
            command.actor.user_id,
            ActivityKind.ORDER_CANCELLED,
            f"Pedido #{saved.number} (S/ {saved.total}): {saved.cancel_reason}",
        )
        announce(self._events, saved)
        return saved
