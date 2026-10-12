"""Propiedades del dinero en pedidos y caja: totales, descuentos, cobros, propinas y arqueo.

Los ejemplos fijan casos conocidos; aquí Hypothesis busca el borde que nadie pensó
(redondeos de céntimos, descuentos que no dividen exacto, cuentas partidas de
mil maneras). Cada propiedad dice una regla del negocio que debe valer siempre.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from resthub.modules.orders.domain.cash import (
    CashPayment,
    CashSession,
    summarize_cash,
)
from resthub.modules.orders.domain.orders import (
    CENT,
    ZERO,
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    PaymentMethod,
)

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)

# Importes en céntimos enteros, para que ningún ejemplo nazca con un decimal de más.
prices = st.integers(min_value=0, max_value=500_000).map(lambda cents: Decimal(cents) * CENT)
percents = st.integers(min_value=0, max_value=10_000).map(
    lambda hundredths: Decimal(hundredths) * CENT
)
tips = st.integers(min_value=0, max_value=100_000).map(lambda cents: Decimal(cents) * CENT)
methods = st.sampled_from(PaymentMethod)


@st.composite
def items(draw: st.DrawFn) -> list[OrderItem]:
    count = draw(st.integers(min_value=1, max_value=8))
    return [
        OrderItem(
            menu_item_id=position,
            name=f"Plato {position}",
            unit_price=draw(prices),
            quantity=draw(st.integers(min_value=1, max_value=20)),
            is_courtesy=draw(st.booleans()),
            id=position,
        )
        for position in range(1, count + 1)
    ]


def make_order(dishes: list[OrderItem], *, status: OrderStatus = OrderStatus.SERVED) -> Order:
    return Order(
        restaurant_id=1,
        number=1,
        business_date=date(2026, 9, 25),
        type=OrderType.DINE_IN,
        waiter_id=7,
        table_id=3,
        items=dishes,
        status=status,
    )


@given(dishes=items(), percent=percents)
def test_order_total_is_dishes_minus_courtesies_minus_discount(
    dishes: list[OrderItem], percent: Decimal
) -> None:
    order = make_order(dishes, status=OrderStatus.OPEN)
    order.apply_discount(percent, "promo", actor_id=1, now=NOW, limit=None)

    chargeable = order.subtotal - order.courtesy_amount
    assert order.total == chargeable - order.discount_amount
    assert ZERO <= order.discount_amount <= chargeable
    assert ZERO <= order.total <= order.subtotal
    assert order.total == order.total.quantize(CENT)


@given(dishes=items(), first=percents, second=percents)
def test_a_bigger_discount_never_raises_the_total(
    dishes: list[OrderItem], first: Decimal, second: Decimal
) -> None:
    low, high = sorted((first, second))
    cheap = make_order(dishes, status=OrderStatus.OPEN)
    dear = make_order([replace(item) for item in dishes], status=OrderStatus.OPEN)
    cheap.apply_discount(high, "promo", actor_id=1, now=NOW, limit=None)
    dear.apply_discount(low, "promo", actor_id=1, now=NOW, limit=None)

    assert cheap.total <= dear.total
    if high == Decimal(100):
        assert cheap.total == ZERO


@given(dishes=items(), percent=percents, data=st.data())
def test_splitting_the_bill_by_dishes_charges_exactly_the_total(
    dishes: list[OrderItem], percent: Decimal, data: st.DataObject
) -> None:
    order = make_order(dishes, status=OrderStatus.OPEN)
    order.apply_discount(percent, "promo", actor_id=1, now=NOW, limit=None)
    order.status = OrderStatus.SERVED
    total = order.total

    # Los platos se reparten entre quienes pagan en el orden y los grupos que salgan.
    ids = data.draw(st.permutations([item.id for item in dishes]))
    cuts = sorted(data.draw(st.sets(st.integers(1, max(len(ids) - 1, 1)), max_size=len(ids))))
    groups = [ids[start:end] for start, end in zip([0, *cuts], [*cuts, len(ids)], strict=False)]
    for group in filter(None, groups):
        if order.status is OrderStatus.PAID:
            break
        payment = order.add_payment(
            PaymentMethod.CARD, NOW, received_by=7, item_ids=[i for i in group if i is not None]
        )
        assert payment.amount >= ZERO

    assert order.status is OrderStatus.PAID
    assert order.balance == ZERO
    assert order.paid_amount == total


@given(dishes=items(), percent=percents, data=st.data())
def test_paying_in_free_parts_never_overcharges_and_ends_at_zero(
    dishes: list[OrderItem], percent: Decimal, data: st.DataObject
) -> None:
    order = make_order(dishes, status=OrderStatus.OPEN)
    order.apply_discount(percent, "promo", actor_id=1, now=NOW, limit=None)
    order.status = OrderStatus.SERVED
    total = order.total

    while order.status is OrderStatus.SERVED and order.balance > ZERO:
        balance = order.balance
        cents = data.draw(st.integers(min_value=1, max_value=int(balance / CENT)))
        order.add_payment(
            PaymentMethod.TRANSFER,
            NOW,
            received_by=7,
            amount=Decimal(cents) * CENT,
            expected_balance=balance,
        )
        assert order.balance == balance - Decimal(cents) * CENT
        assert order.balance >= ZERO

    assert order.paid_amount == total


@given(dishes=items(), tip=tips, extra=tips)
def test_cash_change_is_what_the_customer_handed_over_beyond_bill_and_tip(
    dishes: list[OrderItem], tip: Decimal, extra: Decimal
) -> None:
    order = make_order(dishes)
    due = order.balance
    payment = order.charge(PaymentMethod.CASH, NOW, due + tip + extra, received_by=7, tip=tip)

    assert payment.amount == due
    assert payment.tip == tip
    assert payment.change == extra
    assert order.tips == tip


payment_rows = st.builds(
    CashPayment,
    order_id=st.integers(1, 6),
    order_number=st.integers(1, 6),
    waiter_id=st.integers(1, 3),
    received_by=st.integers(1, 3),
    method=methods,
    amount=prices,
    tip=tips,
    created_at=st.just(NOW),
)


@given(opening=prices, payments=st.lists(payment_rows, max_size=25), shuffler=st.randoms())
def test_cash_summary_adds_up_and_does_not_depend_on_payment_order(
    opening: Decimal, payments: list[CashPayment], shuffler: object
) -> None:
    session = CashSession(restaurant_id=1, opened_by=1, opening_amount=opening)
    summary = summarize_cash(session, payments, [], {})

    assert summary.sales == sum((p.amount for p in payments), ZERO)
    assert sum((row.amount for row in summary.by_method), ZERO) == summary.sales
    assert sum((row.sales for row in summary.by_waiter), ZERO) == summary.sales
    assert sum((row.tips for row in summary.by_waiter), ZERO) == summary.tips
    cash_taken = sum((p.amount + p.tip for p in payments if p.method is PaymentMethod.CASH), ZERO)
    assert summary.expected_cash == opening + cash_taken

    reordered = list(payments)
    shuffler.shuffle(reordered)  # type: ignore[attr-defined]
    assert summarize_cash(session, reordered, [], {}) == summary


@given(opening=prices, expected=prices, counted=prices)
def test_cash_difference_is_counted_minus_expected(
    opening: Decimal, expected: Decimal, counted: Decimal
) -> None:
    session = CashSession(restaurant_id=1, opened_by=1, opening_amount=opening)
    session.close(counted, expected, actor_id=1, now=NOW, notes="")

    assert session.difference == counted - expected
    assert (session.difference == ZERO) == (counted == expected)
