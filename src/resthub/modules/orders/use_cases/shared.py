"""Piezas comunes a los casos de uso de pedidos."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.core.realtime import EventPublisher, RealtimeEvent
from resthub.modules.orders.domain.exceptions import NotYourOrder, OrderNotFound
from resthub.modules.orders.domain.orders import Order
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.ports.staff_directory import StaffDirectory
from resthub.modules.orders.ports.table_repository import TableRepository

# El tablero en vivo y las mesas escuchan este tema. El aviso solo dice qué
# pedido cambió; cada pantalla vuelve a pedirlo con sus propios permisos.
ORDERS_TOPIC = "orders"


def announce(events: EventPublisher, order: Order) -> None:
    # A todo el personal del local y no solo al mesero del pedido: cualquiera
    # puede cubrir una mesa, y el tablero de mesas cambia para todos.
    events.publish(
        RealtimeEvent(
            restaurant_id=order.restaurant_id,
            topic=ORDERS_TOPIC,
            user_ids=frozenset({order.waiter_id}),
            everyone=True,
            reference_id=order.id,
        )
    )


def can_read_all(principal: Principal) -> bool:
    return Permission.ORDERS_READ_ALL in principal.permissions


def is_visible(order: Order, principal: Principal) -> bool:
    """Qué pedidos ve quien pregunta.

    El encargado, todos. El mesero, los suyos y los activos de cualquiera: si
    cubre la mesa de un compañero tiene que poder agregar un plato o marcarla
    servida, pero lo ya cobrado por otro no es asunto suyo.
    """
    return can_read_all(principal) or order.waiter_id == principal.user_id or order.is_active


async def find_visible_order(
    orders: OrderRepository, principal: Principal, order_id: int, *, for_update: bool = False
) -> Order:
    """El pedido si quien pregunta lo puede ver.

    Quien lo va a cambiar lo pide con `for_update`: el pedido queda tomado
    hasta que la transacción termine y un segundo cambio simultáneo espera y
    lee lo ya guardado, en vez de pisarlo.
    """
    order = await orders.get(principal.restaurant_id, order_id, for_update=for_update)
    # El mismo 404 para "no existe", "es de otro local" y "no te toca verlo":
    # distinguirlos delataría qué identificadores existen.
    if order is None or not is_visible(order, principal):
        raise OrderNotFound(order_id)
    return order


def ensure_owns_or_manages(order: Order, principal: Principal, action: str) -> None:
    """Cobrar y descontar son del mesero que tomó el pedido o del encargado.

    Un mesero puede cubrir la mesa de un compañero (agregar platos, servir),
    pero la plata de esa mesa la maneja quien la atendió o el encargado.
    """
    if not can_read_all(principal) and order.waiter_id != principal.user_id:
        raise NotYourOrder(action)


@dataclass(frozen=True, slots=True)
class OrderView:
    """Un pedido con lo que la pantalla necesita para mostrarlo sin más consultas."""

    order: Order
    table_label: str | None
    waiter_name: str
    # Nombres de quienes cobraron o descontaron, para mostrarlos sin otra consulta.
    staff_names: Mapping[int, str] = field(default_factory=dict)


class DescribeOrders:
    """Suma el nombre de la mesa y del mesero a una lista de pedidos.

    Resuelve todo en dos consultas, no una por pedido: el tablero puede tener
    veinte pedidos activos a la vez.
    """

    def __init__(self, tables: TableRepository, staff: StaffDirectory) -> None:
        self._tables = tables
        self._staff = staff

    async def __call__(self, restaurant_id: int, orders: list[Order]) -> list[OrderView]:
        if not orders:
            return []
        labels = {table.id: table.label for table in await self._tables.list_all(restaurant_id)}
        people = {order.waiter_id for order in orders}
        for order in orders:
            people.update(payment.received_by for payment in order.payments)
            if order.discounted_by is not None:
                people.add(order.discounted_by)
        names = await self._staff.names(restaurant_id, people)
        return [
            OrderView(
                order=order,
                table_label=labels.get(order.table_id) if order.table_id is not None else None,
                waiter_name=names.get(order.waiter_id, ""),
                staff_names=names,
            )
            for order in orders
        ]

    async def one(self, restaurant_id: int, order: Order) -> OrderView:
        return (await self(restaurant_id, [order]))[0]
