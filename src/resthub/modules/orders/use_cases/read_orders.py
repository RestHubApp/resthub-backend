from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.modules.orders.domain.orders import Order, OrderStatus, OrderType
from resthub.modules.orders.ports.order_repository import OrderQuery, OrderRepository
from resthub.modules.orders.use_cases.shared import can_read_all, find_visible_order


@dataclass(frozen=True, slots=True)
class ListOrdersQuery:
    actor: Principal
    statuses: frozenset[OrderStatus] | None = None
    date_from: date | None = None
    date_to: date | None = None
    type: OrderType | None = None
    table_id: int | None = None
    waiter_id: int | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class ListOrders:
    """El historial filtrable. El mesero ve los suyos y los activos del local."""

    def __init__(self, orders: OrderRepository) -> None:
        self._orders = orders

    async def __call__(self, query: ListOrdersQuery) -> Page[Order]:
        actor = query.actor
        return await self._orders.search(
            OrderQuery(
                restaurant_id=actor.restaurant_id,
                statuses=query.statuses,
                date_from=query.date_from,
                date_to=query.date_to,
                type=query.type,
                table_id=query.table_id,
                waiter_id=query.waiter_id,
                visible_to=None if can_read_all(actor) else actor.user_id,
                limit=query.limit,
                offset=query.offset,
            )
        )


class ListActiveOrders:
    """Lo que hay en curso en el local, para el tablero de cocina y del salón.

    Todos los activos, también para el mesero: el tablero es compartido.
    """

    def __init__(self, orders: OrderRepository) -> None:
        self._orders = orders

    async def __call__(self, actor: Principal) -> list[Order]:
        return await self._orders.list_active(actor.restaurant_id)


class ReadOrder:
    def __init__(self, orders: OrderRepository) -> None:
        self._orders = orders

    async def __call__(self, actor: Principal, order_id: int) -> Order:
        return await find_visible_order(self._orders, actor, order_id)
