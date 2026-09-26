"""Mover un pedido a otra mesa y unir dos mesas en una sola cuenta.

Exigen `orders.take`: los comensales se mudan o juntan mesas y quien los
atiende lo refleja en el celular. Todo queda en la bitácora.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Principal
from resthub.core.realtime import EventPublisher
from resthub.modules.orders.domain.exceptions import TableInactive, TableOccupied
from resthub.modules.orders.domain.orders import Order
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.ports.restaurant_clock import RestaurantClock
from resthub.modules.orders.ports.table_repository import TableRepository
from resthub.modules.orders.use_cases.manage_tables import find_table
from resthub.modules.orders.use_cases.shared import (
    announce,
    ensure_owns_or_manages,
    find_visible_order,
)


@dataclass(frozen=True, slots=True)
class MoveOrderCommand:
    actor: Principal
    order_id: int
    table_id: int


class MoveOrder:
    def __init__(
        self,
        orders: OrderRepository,
        tables: TableRepository,
        clock: RestaurantClock,
        activity: ActivityRecorder,
        events: EventPublisher,
    ) -> None:
        self._orders = orders
        self._tables = tables
        self._clock = clock
        self._activity = activity
        self._events = events

    async def __call__(self, command: MoveOrderCommand) -> Order:
        actor = command.actor
        # El mismo turno que al abrir un pedido: dos meseros no ocupan a la vez
        # la mesa que se acaba de liberar.
        await self._clock.timezone_for_numbering(actor.restaurant_id)
        order = await find_visible_order(self._orders, actor, command.order_id, for_update=True)
        table = await find_table(self._tables, actor.restaurant_id, command.table_id)
        if not table.is_active:
            raise TableInactive(table.label)
        current = await self._orders.active_for_table(actor.restaurant_id, command.table_id)
        if current is not None and current.id != order.id:
            raise TableOccupied(table.label, current.id or 0)
        before = order.table_id
        order.move_to_table(command.table_id, datetime.now(UTC))
        saved = await self._orders.save(order)
        previous = await self._tables.get(actor.restaurant_id, before or 0)
        await self._activity.record(
            actor.restaurant_id,
            actor.user_id,
            ActivityKind.ORDER_MOVED,
            f"Pedido #{saved.number}: de {previous.label if previous else '?'} a {table.label}",
        )
        announce(self._events, saved)
        return saved


@dataclass(frozen=True, slots=True)
class MergeOrdersCommand:
    actor: Principal
    # El pedido que se queda con todo.
    order_id: int
    # La mesa que se une a él y queda libre.
    source_order_id: int


class MergeOrders:
    def __init__(
        self, orders: OrderRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._orders = orders
        self._activity = activity
        self._events = events

    async def __call__(self, command: MergeOrdersCommand) -> Order:
        actor = command.actor
        # Siempre en el mismo orden, por identificador: dos uniones cruzadas no
        # se esperan una a la otra para siempre.
        first, second = sorted((command.order_id, command.source_order_id))
        locked = {
            order_id: await find_visible_order(self._orders, actor, order_id, for_update=True)
            for order_id in (first, second)
        }
        target, source = locked[command.order_id], locked[command.source_order_id]
        # Unir pasa los platos (y lo que se cobre por ellos) al mesero del
        # pedido que queda: un mesero solo une mesas suyas.
        for order in (target, source):
            ensure_owns_or_manages(order, actor, "unir esas mesas")
        target.absorb(source, datetime.now(UTC))
        saved = await self._orders.save_merge(target, source)
        await self._activity.record(
            actor.restaurant_id,
            actor.user_id,
            ActivityKind.ORDER_MERGED,
            f"Pedido #{source.number} unido al #{saved.number} (S/ {saved.total})",
        )
        announce(self._events, source)
        announce(self._events, saved)
        return saved
