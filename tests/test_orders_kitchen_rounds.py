"""Rondas de cocina y platos anulados: reglas de dominio, sin base ni servidor."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from resthub.modules.orders.domain.exceptions import (
    InvalidOrderError,
    InvalidTransitionError,
    ItemAlreadyVoidedError,
    OrderHasPaymentsError,
    OrderItemNotFoundError,
)
from resthub.modules.orders.domain.orders import (
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    PaymentMethod,
)

NOW = datetime(2026, 10, 15, 20, 0, tzinfo=UTC)
D = Decimal


def _item(price: str = "28.00", quantity: int = 1, item_id: int | None = None) -> OrderItem:
    return OrderItem(
        menu_item_id=1, name="Lomo saltado", unit_price=D(price), quantity=quantity, id=item_id
    )


def _order(*items: OrderItem, table_id: int = 3) -> Order:
    return Order(
        restaurant_id=1,
        number=1,
        business_date=date(2026, 10, 15),
        type=OrderType.DINE_IN,
        waiter_id=7,
        table_id=table_id,
        items=list(items),
    )


def _in_kitchen(*items: OrderItem) -> Order:
    order = _order(*(items or (_item(item_id=1),)))
    order.send_to_kitchen(NOW)
    return order


def _served(*items: OrderItem) -> Order:
    order = _in_kitchen(*items)
    order.mark_ready(NOW)
    order.mark_served(NOW)
    return order


# -- Rondas -------------------------------------------------------------------------------


def test_order_kitchen_round_is_zero_until_sent() -> None:
    order = _order(_item(item_id=1))
    order.add_items([_item(item_id=2)], NOW)

    assert order.kitchen_round == 0
    assert [item.kitchen_round for item in order.items] == [0, 0]
    assert order.current_round_items == []


def test_send_to_kitchen_opens_round_one_with_every_dish() -> None:
    order = _in_kitchen(_item(item_id=1), _item(item_id=2))

    assert order.kitchen_round == 1
    assert [item.kitchen_round for item in order.items] == [1, 1]
    assert [item.id for item in order.current_round_items] == [1, 2]


@pytest.mark.parametrize("status", [OrderStatus.IN_KITCHEN, OrderStatus.READY, OrderStatus.SERVED])
def test_add_items_after_sending_opens_a_new_round(status: OrderStatus) -> None:
    order = _in_kitchen(_item(item_id=1))
    if status is not OrderStatus.IN_KITCHEN:
        order.mark_ready(NOW)
    if status is OrderStatus.SERVED:
        order.mark_served(NOW)

    order.add_items([_item(item_id=2), _item(item_id=3)], NOW)

    assert order.status is OrderStatus.IN_KITCHEN
    assert order.kitchen_round == 2
    assert [item.kitchen_round for item in order.items] == [1, 2, 2]
    assert [item.id for item in order.current_round_items] == [2, 3]


def test_each_batch_after_serving_is_one_more_round() -> None:
    order = _served(_item(item_id=1))
    order.add_items([_item(item_id=2)], NOW)
    order.mark_ready(NOW)
    order.mark_served(NOW)

    order.add_items([_item(item_id=3)], NOW)

    assert order.kitchen_round == 3
    assert [item.kitchen_round for item in order.items] == [1, 2, 3]
    assert [item.id for item in order.current_round_items] == [3]


def test_merging_keeps_the_highest_round_and_each_dishs_round() -> None:
    target = _served(_item(item_id=1))
    target.id = 10
    source = _in_kitchen(_item(item_id=2))
    source.add_items([_item(item_id=3)], NOW)
    source.id = 11

    target.absorb(source, NOW)

    assert target.kitchen_round == 2
    assert [item.kitchen_round for item in target.items] == [1, 1, 2]


def test_open_order_with_merged_dishes_sends_its_own_in_the_next_round() -> None:
    target = _order(_item(item_id=1), table_id=3)
    target.id = 10
    source = _in_kitchen(_item(item_id=2))
    source.id = 11
    target.absorb(source, NOW)

    target.send_to_kitchen(NOW)

    assert target.kitchen_round == 2
    assert [item.kitchen_round for item in target.items] == [2, 1]
    assert [item.id for item in target.current_round_items] == [1]


# -- Anular un plato ------------------------------------------------------------------------


@pytest.mark.parametrize("status", [OrderStatus.IN_KITCHEN, OrderStatus.READY, OrderStatus.SERVED])
def test_void_item_after_sending_marks_it_and_keeps_it(status: OrderStatus) -> None:
    order = _in_kitchen(_item("28.00", item_id=1), _item("10.00", 2, item_id=2))
    if status is not OrderStatus.IN_KITCHEN:
        order.mark_ready(NOW)
    if status is OrderStatus.SERVED:
        order.mark_served(NOW)

    voided = order.void_item(2, "  El cliente  se arrepintió ", NOW)

    assert voided.voided_at == NOW
    assert voided.void_reason == "El cliente se arrepintió"
    assert order.status is status
    assert [item.id for item in order.items] == [1, 2]
    assert [item.id for item in order.live_items] == [1]
    assert order.updated_at == NOW


def test_voided_item_leaves_subtotal_total_count_and_round() -> None:
    order = _in_kitchen(_item("28.00", item_id=1), _item("10.00", 2, item_id=2))

    order.void_item(2, "Se cayó", NOW)

    assert order.subtotal == D("28.00")
    assert order.total == D("28.00")
    assert order.balance == D("28.00")
    assert order.item_count == 1
    assert [item.id for item in order.current_round_items] == [1]


def test_voided_item_does_not_count_in_discount_or_courtesies() -> None:
    order = _in_kitchen(_item("100.00", item_id=1), _item("50.00", item_id=2))
    order.grant_courtesy(2, "Invita la casa", NOW)
    order.apply_discount(D(10), "Cliente frecuente", 9, NOW, None)

    order.void_item(2, "No salió", NOW)

    assert order.courtesy_amount == D("0.00")
    assert order.discount_amount == D("10.00")
    assert order.total == D("90.00")


def test_void_item_of_the_only_dish_leaves_the_order_as_is() -> None:
    order = _served(_item(item_id=1))

    order.void_item(1, "Mal preparado", NOW)

    assert order.status is OrderStatus.SERVED
    assert order.total == D("0.00")
    assert order.item_count == 0


def test_void_item_in_open_order_is_rejected() -> None:
    order = _order(_item(item_id=1))

    with pytest.raises(InvalidTransitionError):
        order.void_item(1, "Se fue", NOW)


def test_void_item_in_closed_order_is_rejected() -> None:
    order = _in_kitchen(_item(item_id=1))
    order.cancel("Se fueron", NOW)

    with pytest.raises(InvalidTransitionError):
        order.void_item(1, "Se fue", NOW)


def test_void_item_with_payments_is_rejected() -> None:
    order = _served(_item("28.00", item_id=1), _item("10.00", item_id=2))
    order.add_payment(
        PaymentMethod.CASH, NOW, received_by=7, amount=D("10.00"), expected_balance=D("38.00")
    )

    with pytest.raises(OrderHasPaymentsError):
        order.void_item(2, "Se fue", NOW)
    assert not order.items[1].is_voided


def test_void_item_twice_is_rejected() -> None:
    order = _in_kitchen(_item(item_id=1), _item(item_id=2))
    order.void_item(1, "Se fue", NOW)

    with pytest.raises(ItemAlreadyVoidedError):
        order.void_item(1, "Otra vez", NOW)


def test_void_item_needs_a_reason_and_an_existing_dish() -> None:
    order = _in_kitchen(_item(item_id=1))

    with pytest.raises(InvalidOrderError):
        order.void_item(1, "   ", NOW)
    with pytest.raises(OrderItemNotFoundError):
        order.void_item(99, "Se fue", NOW)
    assert not order.items[0].is_voided


def test_voided_item_cannot_be_invited_or_paid() -> None:
    order = _served(_item("28.00", item_id=1), _item("10.00", item_id=2))
    order.void_item(2, "Se fue", NOW)

    with pytest.raises(ItemAlreadyVoidedError):
        order.grant_courtesy(2, "Invita la casa", NOW)
    with pytest.raises(ItemAlreadyVoidedError):
        order.revoke_courtesy(2, NOW)
    with pytest.raises(ItemAlreadyVoidedError):
        order.add_payment(PaymentMethod.CASH, NOW, received_by=7, item_ids=[2])


def test_paying_the_last_live_dishes_closes_the_bill_despite_voided_ones() -> None:
    order = _served(_item("28.00", item_id=1), _item("10.00", item_id=2))
    order.void_item(2, "Se fue", NOW)

    payment = order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[1])

    assert payment.amount == D("28.00")
    assert order.status is OrderStatus.PAID


def test_charging_after_voiding_charges_only_live_dishes() -> None:
    order = _served(_item("28.00", item_id=1), _item("10.00", 3, item_id=2))
    order.void_item(2, "Se fue", NOW)

    payment = order.charge(PaymentMethod.CARD, NOW)

    assert payment.amount == D("28.00")
    assert order.status is OrderStatus.PAID
