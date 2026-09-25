"""Traducción entre las filas de las tablas y las entidades de dominio."""

from __future__ import annotations

from resthub.core.timestamps import as_utc
from resthub.modules.orders.adapters.persistence.models import (
    DiningTableRow,
    OrderItemRow,
    OrderRow,
)
from resthub.modules.orders.domain.orders import (
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    PaymentMethod,
)
from resthub.modules.orders.domain.tables import DiningTable


def table_to_entity(row: DiningTableRow) -> DiningTable:
    return DiningTable(
        id=row.id,
        restaurant_id=row.restaurant_id,
        label=row.label,
        position=row.position,
        is_active=row.is_active,
        created_at=as_utc(row.created_at),
    )


def table_to_row(table: DiningTable) -> DiningTableRow:
    return DiningTableRow(
        restaurant_id=table.restaurant_id,
        label=table.label,
        position=table.position,
        is_active=table.is_active,
        created_at=table.created_at,
    )


def item_to_entity(row: OrderItemRow) -> OrderItem:
    return OrderItem(
        id=row.id,
        menu_item_id=row.menu_item_id,
        name=row.name,
        unit_price=row.unit_price,
        quantity=row.quantity,
        notes=row.notes,
        created_at=as_utc(row.created_at),
    )


def item_to_row(item: OrderItem, restaurant_id: int) -> OrderItemRow:
    return OrderItemRow(
        restaurant_id=restaurant_id,
        menu_item_id=item.menu_item_id,
        name=item.name,
        unit_price=item.unit_price,
        quantity=item.quantity,
        notes=item.notes,
        created_at=item.created_at,
    )


def order_to_entity(row: OrderRow) -> Order:
    return Order(
        id=row.id,
        restaurant_id=row.restaurant_id,
        number=row.number,
        business_date=row.business_date,
        type=OrderType(row.type),
        waiter_id=row.waiter_id,
        table_id=row.table_id,
        customer_name=row.customer_name,
        notes=row.notes,
        status=OrderStatus(row.status),
        items=[item_to_entity(item) for item in sorted(row.items, key=lambda item: item.id)],
        cancel_reason=row.cancel_reason,
        payment_method=PaymentMethod(row.payment_method) if row.payment_method else None,
        amount_received=row.amount_received,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        paid_at=as_utc(row.paid_at) if row.paid_at else None,
        cancelled_at=as_utc(row.cancelled_at) if row.cancelled_at else None,
    )


def order_to_row(order: Order) -> OrderRow:
    row = OrderRow(
        restaurant_id=order.restaurant_id,
        number=order.number,
        business_date=order.business_date,
        type=order.type.value,
        created_at=order.created_at,
        waiter_id=order.waiter_id,
        table_id=order.table_id,
        items=[item_to_row(item, order.restaurant_id) for item in order.items],
    )
    copy_order_state(order, row)
    return row


def copy_order_state(order: Order, row: OrderRow) -> None:
    """Lo que puede cambiar en la vida de un pedido; el resto se fija al abrirlo."""
    row.status = order.status.value
    row.customer_name = order.customer_name
    row.notes = order.notes
    row.total = order.total
    row.cancel_reason = order.cancel_reason
    row.payment_method = order.payment_method.value if order.payment_method else None
    row.amount_received = order.amount_received
    row.updated_at = order.updated_at
    row.paid_at = order.paid_at
    row.cancelled_at = order.cancelled_at
