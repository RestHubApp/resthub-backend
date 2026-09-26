"""Traducción entre las filas de las tablas y las entidades de dominio."""

from __future__ import annotations

from decimal import Decimal

from resthub.core.timestamps import as_utc
from resthub.modules.orders.adapters.persistence.models import (
    CashSessionRow,
    DiningTableRow,
    OrderItemRow,
    OrderRow,
    PaymentRow,
)
from resthub.modules.orders.domain.cash import CashSession
from resthub.modules.orders.domain.modifiers import ChosenModifier
from resthub.modules.orders.domain.orders import (
    MIXED_PAYMENT,
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    Payment,
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
        modifiers=tuple(
            ChosenModifier(group=m["group"], option=m["option"], price=Decimal(m["price"]))
            for m in row.modifiers or []
        ),
        is_courtesy=row.is_courtesy,
        courtesy_reason=row.courtesy_reason,
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
        # El precio como texto: JSON no tiene decimales exactos.
        modifiers=[
            {"group": m.group, "option": m.option, "price": str(m.price)} for m in item.modifiers
        ],
        is_courtesy=item.is_courtesy,
        courtesy_reason=item.courtesy_reason,
        created_at=item.created_at,
    )


def payment_to_entity(row: PaymentRow, items: list[OrderItemRow]) -> Payment:
    return Payment(
        id=row.id,
        method=PaymentMethod(row.method),
        amount=row.amount,
        tip=row.tip,
        amount_received=row.amount_received,
        received_by=row.received_by,
        cash_session_id=row.cash_session_id,
        item_ids=tuple(item.id for item in items if item.payment_id == row.id),
        created_at=as_utc(row.created_at),
    )


def payment_to_row(payment: Payment, restaurant_id: int) -> PaymentRow:
    return PaymentRow(
        restaurant_id=restaurant_id,
        cash_session_id=payment.cash_session_id,
        method=payment.method.value,
        amount=payment.amount,
        tip=payment.tip,
        amount_received=payment.amount_received,
        received_by=payment.received_by,
        created_at=payment.created_at,
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
        customer_phone=row.customer_phone,
        delivery_address=row.delivery_address,
        delivery_reference=row.delivery_reference,
        customer_id=row.customer_id,
        client_request_id=row.client_request_id,
        notes=row.notes,
        status=OrderStatus(row.status),
        items=[item_to_entity(item) for item in sorted(row.items, key=lambda item: item.id)],
        cancel_reason=row.cancel_reason,
        discount_percent=row.discount_percent,
        discount_reason=row.discount_reason,
        discounted_by=row.discounted_by,
        payments=[payment_to_entity(payment, row.items) for payment in row.payments],
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        status_changed_at=as_utc(row.status_changed_at),
        paid_at=as_utc(row.paid_at) if row.paid_at else None,
        cancelled_at=as_utc(row.cancelled_at) if row.cancelled_at else None,
        merged_into_id=row.merged_into_id,
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
        client_request_id=order.client_request_id,
        items=[item_to_row(item, order.restaurant_id) for item in order.items],
        # Siempre cargada, aunque vacía: leerla después del `flush` dispararía
        # una carga perezosa, que en asíncrono falla.
        payments=[payment_to_row(payment, order.restaurant_id) for payment in order.payments],
    )
    copy_order_state(order, row)
    return row


def copy_order_state(order: Order, row: OrderRow) -> None:
    """Lo que puede cambiar en la vida de un pedido; el resto se fija al abrirlo."""
    row.status = order.status.value
    row.table_id = order.table_id
    row.customer_name = order.customer_name
    row.customer_phone = order.customer_phone
    row.delivery_address = order.delivery_address
    row.delivery_reference = order.delivery_reference
    row.customer_id = order.customer_id
    row.notes = order.notes
    row.total = order.total
    row.cancel_reason = order.cancel_reason
    row.discount_percent = order.discount_percent
    row.discount_reason = order.discount_reason
    row.discounted_by = order.discounted_by
    row.discount_amount = order.discount_amount
    row.courtesy_amount = order.courtesy_amount
    if order.is_mixed_payment:
        row.payment_method = MIXED_PAYMENT
    else:
        row.payment_method = order.payment_method.value if order.payment_method else None
    row.amount_received = order.amount_received
    row.updated_at = order.updated_at
    row.status_changed_at = order.status_changed_at or order.created_at
    row.paid_at = order.paid_at
    row.cancelled_at = order.cancelled_at
    row.merged_into_id = order.merged_into_id


def cash_session_to_entity(row: CashSessionRow) -> CashSession:
    return CashSession(
        id=row.id,
        restaurant_id=row.restaurant_id,
        opened_by=row.opened_by,
        opening_amount=row.opening_amount,
        opening_notes=row.opening_notes,
        opened_at=as_utc(row.opened_at),
        closed_by=row.closed_by,
        closed_at=as_utc(row.closed_at) if row.closed_at else None,
        counted_cash=row.counted_cash,
        expected_cash=row.expected_cash,
        closing_notes=row.closing_notes,
    )


def copy_cash_session_state(session: CashSession, row: CashSessionRow) -> None:
    row.restaurant_id = session.restaurant_id
    row.opened_by = session.opened_by
    row.opening_amount = session.opening_amount
    row.opening_notes = session.opening_notes
    row.opened_at = session.opened_at
    row.closed_by = session.closed_by
    row.closed_at = session.closed_at
    row.counted_cash = session.counted_cash
    row.expected_cash = session.expected_cash
    row.closing_notes = session.closing_notes
