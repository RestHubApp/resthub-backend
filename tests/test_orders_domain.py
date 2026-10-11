"""Reglas de dominio de los pedidos, sin base ni servidor."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from resthub.modules.orders.domain.exceptions import (
    BalanceChangedError,
    DiscountNotAllowedError,
    InvalidOrderError,
    InvalidTransitionError,
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

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)


def _item(price: str = "28.00", quantity: int = 1, item_id: int | None = None) -> OrderItem:
    return OrderItem(
        menu_item_id=1,
        name="Lomo saltado",
        unit_price=Decimal(price),
        quantity=quantity,
        id=item_id,
    )


def _order(*items: OrderItem) -> Order:
    return Order(
        restaurant_id=1,
        number=1,
        business_date=date(2026, 9, 25),
        type=OrderType.DINE_IN,
        waiter_id=7,
        table_id=3,
        items=list(items),
    )


def _served(*items: OrderItem) -> Order:
    order = _order(*(items or (_item(),)))
    order.send_to_kitchen(NOW)
    order.mark_ready(NOW)
    order.mark_served(NOW)
    return order


def test_total_sums_price_times_quantity() -> None:
    order = _order(_item("28.00", 2), _item("5.50", 3))

    assert order.total == Decimal("72.50")
    assert order.item_count == 5


def test_empty_order_totals_zero() -> None:
    assert _order().total == Decimal("0.00")


def test_full_cycle() -> None:
    order = _order(_item())

    order.send_to_kitchen(NOW)
    assert order.status is OrderStatus.IN_KITCHEN
    order.mark_ready(NOW)
    assert order.status is OrderStatus.READY
    order.mark_served(NOW)
    assert order.status is OrderStatus.SERVED
    order.charge(PaymentMethod.YAPE, NOW)

    assert order.status is OrderStatus.PAID
    assert order.paid_at == NOW
    assert not order.is_active


@pytest.mark.parametrize(
    ("setup", "action"),
    [
        ([], "mark_ready"),
        ([], "mark_served"),
        (["send_to_kitchen"], "mark_served"),
        (["send_to_kitchen"], "send_to_kitchen"),
        (["send_to_kitchen", "mark_ready"], "mark_ready"),
    ],
)
def test_transitions_that_skip_steps_are_rejected(setup: list[str], action: str) -> None:
    order = _order(_item())
    for step in setup:
        getattr(order, step)(NOW)

    with pytest.raises(InvalidTransitionError):
        getattr(order, action)(NOW)


def test_only_served_orders_are_charged() -> None:
    order = _order(_item())
    order.send_to_kitchen(NOW)
    order.mark_ready(NOW)

    with pytest.raises(InvalidTransitionError):
        order.charge(PaymentMethod.CASH, NOW)


def test_order_without_dishes_does_not_go_to_kitchen() -> None:
    with pytest.raises(InvalidOrderError):
        _order().send_to_kitchen(NOW)


@pytest.mark.parametrize("until", ["mark_ready", "mark_served"])
def test_adding_dishes_to_ready_or_served_order_returns_it_to_kitchen(until: str) -> None:
    order = _order(_item())
    order.send_to_kitchen(NOW)
    order.mark_ready(NOW)
    if until == "mark_served":
        order.mark_served(NOW)

    order.add_items([_item("5.00")], NOW)

    assert order.status is OrderStatus.IN_KITCHEN
    assert order.total == Decimal("33.00")


def test_adding_to_open_or_in_kitchen_order_keeps_its_status() -> None:
    open_order = _order()
    in_kitchen_order = _order(_item())
    in_kitchen_order.send_to_kitchen(NOW)

    open_order.add_items([_item()], NOW)
    in_kitchen_order.add_items([_item()], NOW)

    assert open_order.status is OrderStatus.OPEN
    assert in_kitchen_order.status is OrderStatus.IN_KITCHEN


def test_status_timestamp_only_moves_on_a_status_change() -> None:
    opened_at = datetime(2026, 9, 25, 19, 0, tzinfo=UTC)
    order = Order(
        restaurant_id=1,
        number=1,
        business_date=date(2026, 9, 25),
        type=OrderType.TAKEAWAY,
        waiter_id=7,
        items=[_item()],
        created_at=opened_at,
    )
    assert order.status_changed_at == opened_at

    order.send_to_kitchen(NOW)
    order.update_details(NOW + timedelta(minutes=3), notes="sin cebolla")
    order.add_items([_item()], NOW + timedelta(minutes=5))

    # Editar y sumar platos a lo que ya está en cocina no reinicia el reloj.
    assert order.status_changed_at == NOW
    assert order.updated_at == NOW + timedelta(minutes=5)

    order.mark_ready(NOW + timedelta(minutes=12))
    assert order.status_changed_at == NOW + timedelta(minutes=12)

    # Volver a cocina por un plato nuevo sí es un cambio de estado.
    order.add_items([_item()], NOW + timedelta(minutes=14))
    assert order.status is OrderStatus.IN_KITCHEN
    assert order.status_changed_at == NOW + timedelta(minutes=14)


def test_nothing_is_added_to_a_closed_order() -> None:
    order = _served()
    order.charge(PaymentMethod.CARD, NOW)

    with pytest.raises(InvalidTransitionError):
        order.add_items([_item()], NOW)


def test_removing_and_changing_dishes_only_while_order_is_open() -> None:
    order = _order(_item(item_id=1), _item(item_id=2))
    order.change_item(1, NOW, quantity=3, notes="sin  cebolla")
    order.remove_item(2, NOW)

    assert [(i.id, i.quantity, i.notes) for i in order.items] == [(1, 3, "sin cebolla")]
    with pytest.raises(OrderItemNotFoundError):
        order.remove_item(99, NOW)

    order.send_to_kitchen(NOW)
    with pytest.raises(InvalidTransitionError):
        order.change_item(1, NOW, quantity=1)
    with pytest.raises(InvalidTransitionError):
        order.remove_item(1, NOW)


@pytest.mark.parametrize("quantity", [0, 100])
def test_quantity_has_limits(quantity: int) -> None:
    with pytest.raises(InvalidOrderError):
        _item(quantity=quantity)


def test_cancelling_requires_a_reason() -> None:
    order = _order(_item())

    with pytest.raises(InvalidOrderError):
        order.cancel("   ", NOW)
    order.cancel("  El cliente se  fue ", NOW)

    assert order.status is OrderStatus.CANCELLED
    assert order.cancel_reason == "El cliente se fue"
    assert order.cancelled_at == NOW


def test_paid_order_is_not_cancelled() -> None:
    order = _served()
    order.charge(PaymentMethod.CASH, NOW)

    with pytest.raises(InvalidTransitionError):
        order.cancel("Error", NOW)


def test_change_is_computed_for_cash() -> None:
    order = _served(_item("28.00"), _item("5.50", 2))

    order.charge(PaymentMethod.CASH, NOW, amount_received=Decimal(50))

    assert order.total == Decimal("39.00")
    assert order.amount_received == Decimal("50.00")
    assert order.change == Decimal("11.00")


def test_cash_without_amount_means_exact_payment() -> None:
    order = _served()

    order.charge(PaymentMethod.CASH, NOW)

    assert order.change == Decimal("0.00")


def test_cash_must_cover_the_total() -> None:
    order = _served()

    with pytest.raises(InvalidOrderError):
        order.charge(PaymentMethod.CASH, NOW, amount_received=Decimal("20.00"))
    assert order.status is OrderStatus.SERVED


def test_other_method_has_no_amount_nor_change() -> None:
    order = _served()

    with pytest.raises(InvalidOrderError):
        order.charge(PaymentMethod.YAPE, NOW, amount_received=Decimal("50.00"))
    order.charge(PaymentMethod.PLIN, NOW)

    assert order.change is None
    assert order.payment_method is PaymentMethod.PLIN


def test_dine_in_needs_a_table_and_takeaway_has_none() -> None:
    with pytest.raises(InvalidOrderError):
        Order(
            restaurant_id=1,
            number=1,
            business_date=date(2026, 9, 25),
            type=OrderType.DINE_IN,
            waiter_id=1,
        )
    with pytest.raises(InvalidOrderError):
        Order(
            restaurant_id=1,
            number=1,
            business_date=date(2026, 9, 25),
            type=OrderType.TAKEAWAY,
            waiter_id=1,
            table_id=2,
        )


# -- Descuentos, cortesías y pagos parciales -----------------------------------


def test_discount_rounds_to_the_cent_and_leaves_courtesies_alone() -> None:
    order = _served(_item("33.33", 1, item_id=1), _item("10.00", 1, item_id=2))
    order.grant_courtesy(2, "Invita la casa", NOW)

    order.apply_discount(Decimal("12.5"), "Cliente frecuente", 5, NOW, limit=None)

    # 12.5 % de 33.33 es 4.16625: se redondea a 4.17.
    assert order.courtesy_amount == Decimal("10.00")
    assert order.discount_amount == Decimal("4.17")
    assert order.total == Decimal("29.16")


def test_discount_above_the_limit_is_not_applied() -> None:
    order = _served()

    with pytest.raises(DiscountNotAllowedError):
        order.apply_discount(Decimal(15), "Amigo", 7, NOW, limit=Decimal(10))

    assert order.discount_percent == 0


def test_whoever_pays_the_last_dishes_closes_the_bill_with_the_rounding() -> None:
    order = _served(_item("10.00", 1, item_id=1), _item("10.00", 1, item_id=2))
    order.apply_discount(Decimal("33.33"), "Promoción", 5, NOW, limit=None)

    first = order.add_payment(PaymentMethod.CASH, NOW, received_by=7, item_ids=[1])
    second = order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[2])

    # 20.00 con 33.33 % de descuento son 13.33. Cada plato solo daría 6.67 y
    # sobraría un céntimo: el último pago toma lo que falta, 6.66.
    assert order.total == Decimal("13.33")
    assert first.amount == Decimal("6.67")
    assert second.amount == Decimal("6.66")
    assert order.balance == 0
    assert order.status is OrderStatus.PAID


def test_fully_comped_bill_closes_with_zero_amount() -> None:
    order = _served(_item("28.00", 1, item_id=1))
    order.grant_courtesy(1, "Error de cocina", NOW)

    payment = order.charge(PaymentMethod.CASH, NOW, received_by=5)

    assert payment.amount == Decimal("0.00")
    assert order.status is OrderStatus.PAID


def test_order_with_a_payment_cannot_be_discounted_or_cancelled() -> None:
    order = _served(_item("28.00", 2, item_id=1))
    order.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=Decimal(10), expected_balance=order.balance
    )

    with pytest.raises(OrderHasPaymentsError):
        order.apply_discount(Decimal(5), "Tarde", 7, NOW, limit=None)
    with pytest.raises(OrderHasPaymentsError):
        order.cancel("Se fueron", NOW)
    assert order.balance == Decimal("46.00")
    assert order.status is OrderStatus.SERVED


def test_different_expected_balance_blocks_the_charge() -> None:
    order = _served()

    with pytest.raises(BalanceChangedError):
        order.add_payment(PaymentMethod.CASH, NOW, received_by=7, expected_balance=Decimal("20.00"))
    assert order.payments == []


def test_repeated_free_part_is_not_charged_twice() -> None:
    order = _served(_item("28.00", 2, item_id=1))
    seen_balance = order.balance
    order.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=Decimal(10), expected_balance=seen_balance
    )

    # El reintento llega con el saldo de antes del primer pago.
    with pytest.raises(BalanceChangedError):
        order.add_payment(
            PaymentMethod.YAPE,
            NOW,
            received_by=7,
            amount=Decimal(10),
            expected_balance=seen_balance,
        )
    # Sin el saldo que se vio no hay forma de saber si es un reintento.
    with pytest.raises(InvalidOrderError):
        order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, amount=Decimal(10))
    assert len(order.payments) == 1
    assert order.balance == Decimal("46.00")
