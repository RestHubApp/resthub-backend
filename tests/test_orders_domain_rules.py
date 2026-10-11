"""Límites, mensajes y caminos de error del dominio de pedidos, caja y mesas.

Complementa `test_orders_domain.py`. Cada caso fija el primer valor que se
rechaza y el último que se acepta, el mensaje exacto que ve quien opera y que
un intento fallido no deje el pedido a medio cambiar.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from resthub.modules.orders.domain.cash import (
    MAX_CASH_AMOUNT,
    CashOrderAdjustment,
    CashPayment,
    CashSession,
    summarize_cash,
    validate_cash_amount,
)
from resthub.modules.orders.domain.exceptions import (
    BalanceChangedError,
    CashRegisterAlreadyOpenError,
    CashRegisterClosedError,
    CashSessionNotFoundError,
    CustomerNotFoundError,
    DiscountNotAllowedError,
    DishNotFoundError,
    DishUnavailableError,
    InvalidCashSessionError,
    InvalidOrderError,
    InvalidTableError,
    InvalidTableOrderingError,
    InvalidTransitionError,
    NotYourOrderError,
    OrderHasPaymentsError,
    OrderItemNotFoundError,
    OrderNotFoundError,
    OrderNumberTakenError,
    TableInactiveError,
    TableLabelTakenError,
    TableNotFoundError,
    TableOccupiedError,
)
from resthub.modules.orders.domain.modifiers import (
    MAX_CHOSEN,
    DishOption,
    DishOptionGroup,
    choose_modifiers,
)
from resthub.modules.orders.domain.orders import (
    MAX_ADDRESS_LENGTH,
    MAX_CLIENT_REQUEST_ID_LENGTH,
    MAX_CUSTOMER_NAME_LENGTH,
    MAX_DISH_NAME_LENGTH,
    MAX_ITEM_NOTES_LENGTH,
    MAX_ORDER_NOTES_LENGTH,
    MAX_PHONE_LENGTH,
    MAX_QUANTITY,
    MAX_REFERENCE_LENGTH,
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    Payment,
    PaymentMethod,
    validate_adjustment_reason,
)
from resthub.modules.orders.domain.tables import (
    MAX_LABEL_LENGTH,
    DiningTable,
    TableStatus,
    reorder_tables,
)

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
D = Decimal


def _message(error: pytest.ExceptionInfo[BaseException]) -> str:
    return str(error.value)


def _item(
    price: str = "28.00", quantity: int = 1, item_id: int | None = None, **extra: object
) -> OrderItem:
    fields: dict[str, object] = {"menu_item_id": 1, "name": "Lomo saltado"}
    fields.update(extra)
    return OrderItem(unit_price=D(price), quantity=quantity, id=item_id, **fields)  # type: ignore[arg-type]


def _order(*items: OrderItem, **extra: object) -> Order:
    fields: dict[str, object] = {
        "restaurant_id": 1,
        "number": 14,
        "business_date": date(2026, 9, 25),
        "type": OrderType.DINE_IN,
        "waiter_id": 7,
        "table_id": 3,
        "items": list(items),
    }
    fields.update(extra)
    return Order(**fields)  # type: ignore[arg-type]


def _delivery(**extra: object) -> Order:
    fields: dict[str, object] = {
        "type": OrderType.DELIVERY,
        "table_id": None,
        "customer_name": "Ana",
        "customer_phone": "987654321",
        "delivery_address": "Jr. Pizarro 450",
    }
    fields.update(extra)
    return _order(_item(item_id=1), **fields)


def _served(*items: OrderItem, **extra: object) -> Order:
    order = _order(*(items or (_item(item_id=1),)), **extra)
    order.send_to_kitchen(NOW)
    order.mark_ready(NOW)
    order.mark_served(NOW)
    return order


# -- Rótulos --------------------------------------------------------------------------


def test_labels_for_types_statuses_methods_and_tables() -> None:
    assert [t.label for t in OrderType] == ["En mesa", "Para llevar", "Delivery"]
    assert [s.label for s in OrderStatus] == [
        "Abierto",
        "En cocina",
        "Listo",
        "Servido",
        "Pagado",
        "Cancelado",
    ]
    assert [s for s in OrderStatus if s.is_active] == [
        OrderStatus.OPEN,
        OrderStatus.IN_KITCHEN,
        OrderStatus.READY,
        OrderStatus.SERVED,
    ]
    assert [m.label for m in PaymentMethod] == [
        "Efectivo",
        "Yape",
        "Plin",
        "Tarjeta",
        "Transferencia",
    ]
    assert [s.label for s in TableStatus] == ["Libre", "Ocupada"]


# -- Platos del pedido ------------------------------------------------------------------


def test_dish_freezes_name_and_price_with_cents() -> None:
    dish = _item("12.5", 2, name="  Lomo saltado  ", notes="  sin   cebolla ")

    assert dish.name == "Lomo saltado"
    assert str(dish.unit_price) == "12.50"
    assert dish.notes == "sin cebolla"
    assert dish.subtotal == D("25.00")
    assert dish.due == D("25.00")


def test_dish_name_is_trimmed_to_the_maximum() -> None:
    dish = _item(name="L" * (MAX_DISH_NAME_LENGTH + 10))

    assert len(dish.name) == MAX_DISH_NAME_LENGTH


def test_zero_price_dish_is_accepted_and_negative_is_not() -> None:
    assert _item("0").subtotal == D("0.00")
    with pytest.raises(InvalidOrderError) as error:
        _item("-0.01")
    assert _message(error) == "El precio de un plato no puede ser negativo."


@pytest.mark.parametrize("quantity", [0, -1, MAX_QUANTITY + 1])
def test_quantity_ranges_from_one_to_ninety_nine(quantity: int) -> None:
    with pytest.raises(InvalidOrderError) as error:
        _item(quantity=quantity)
    assert _message(error) == "La cantidad tiene que estar entre 1 y 99."


def test_boundary_quantities_are_accepted() -> None:
    assert _item(quantity=1).quantity == 1
    assert _item(quantity=MAX_QUANTITY).quantity == 99


def test_dish_note_has_a_limit() -> None:
    assert len(_item(notes="n" * MAX_ITEM_NOTES_LENGTH).notes) == MAX_ITEM_NOTES_LENGTH
    with pytest.raises(InvalidOrderError) as error:
        _item(notes="n" * (MAX_ITEM_NOTES_LENGTH + 1))
    assert _message(error) == "La nota del plato admite 200 caracteres."


def test_complimentary_item_is_not_charged_but_adds_to_subtotal() -> None:
    dish = _item("10.00", 2, is_courtesy=True)

    assert dish.subtotal == D("20.00")
    assert dish.due == D("0.00")


# -- Datos del pedido -----------------------------------------------------------------


def test_order_cleans_its_texts_and_trims_the_identifier() -> None:
    order = _order(
        customer_name="  Ana   Torres ",
        notes="  sin ají  ",
        client_request_id="  " + "x" * (MAX_CLIENT_REQUEST_ID_LENGTH + 5) + "  ",
    )

    assert order.customer_name == "Ana Torres"
    assert order.notes == "sin ají"
    assert order.client_request_id == "x" * MAX_CLIENT_REQUEST_ID_LENGTH
    assert order.status_changed_at == order.created_at


def test_without_retry_identifier_it_stays_empty() -> None:
    assert _order().client_request_id is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"table_id": None}, "Un pedido en mesa necesita la mesa."),
        ({"type": OrderType.TAKEAWAY}, "Un pedido para llevar o delivery no ocupa mesa."),
        ({"type": OrderType.DELIVERY}, "Un pedido para llevar o delivery no ocupa mesa."),
        (
            {"customer_name": "N" * (MAX_CUSTOMER_NAME_LENGTH + 1)},
            "El nombre del cliente admite 80 caracteres.",
        ),
        (
            {"customer_phone": "9" * (MAX_PHONE_LENGTH + 1)},
            "El teléfono admite 20 caracteres como máximo.",
        ),
        (
            {"delivery_address": "D" * (MAX_ADDRESS_LENGTH + 1)},
            "La dirección admite 200 caracteres como máximo.",
        ),
        (
            {"delivery_reference": "R" * (MAX_REFERENCE_LENGTH + 1)},
            "La referencia admite 150 caracteres como máximo.",
        ),
        (
            {"notes": "N" * (MAX_ORDER_NOTES_LENGTH + 1)},
            "La nota del pedido admite 300 caracteres.",
        ),
    ],
)
def test_invalid_order_explains_the_reason(data: dict[str, object], message: str) -> None:
    with pytest.raises(InvalidOrderError) as error:
        _order(**data)
    assert _message(error) == message


def test_order_maximum_lengths_are_accepted() -> None:
    order = _order(
        customer_name="N" * MAX_CUSTOMER_NAME_LENGTH,
        customer_phone="9" * MAX_PHONE_LENGTH,
        delivery_address="D" * MAX_ADDRESS_LENGTH,
        delivery_reference="R" * MAX_REFERENCE_LENGTH,
        notes="N" * MAX_ORDER_NOTES_LENGTH,
    )

    assert len(order.customer_name) == 80
    assert len(order.customer_phone) == 20
    assert len(order.delivery_address) == 200
    assert len(order.delivery_reference) == 150
    assert len(order.notes) == 300


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ({"customer_name": " "}, "Un delivery necesita el nombre del cliente."),
        ({"customer_phone": ""}, "Un delivery necesita un teléfono para coordinar la entrega."),
        ({"delivery_address": "  "}, "Un delivery necesita la dirección de entrega."),
    ],
)
def test_delivery_needs_recipient_and_destination(missing: dict[str, str], message: str) -> None:
    with pytest.raises(InvalidOrderError) as error:
        _delivery(**missing)
    assert _message(error) == message


@pytest.mark.parametrize("status", [OrderStatus.PAID, OrderStatus.CANCELLED])
def test_closed_delivery_is_readable_even_if_customer_erased_their_data(
    status: OrderStatus,
) -> None:
    # Derecho de cancelación (Ley N.º 29733): sin teléfono ni dirección, el
    # pedido cobrado o cancelado tiene que poder leerse para los reportes.
    order = _delivery(
        status=status, customer_name="Cliente eliminado", customer_phone="", delivery_address=""
    )

    assert order.status is status


def test_complete_delivery_is_accepted() -> None:
    order = _delivery(delivery_reference=" frente al parque ")

    assert order.table_id is None
    assert order.delivery_reference == "frente al parque"


def test_updating_details_changes_only_what_was_sent() -> None:
    order = _order(notes="sin ají", customer_name="Ana")

    order.update_details(LATER, customer_name="  Luis  ")

    assert (order.notes, order.customer_name) == ("sin ají", "Luis")
    assert order.updated_at == LATER
    order.update_details(LATER, notes="  con  hielo ")
    assert (order.notes, order.customer_name) == ("con  hielo", "Luis")


def test_updating_delivery_changes_each_field_separately() -> None:
    order = _delivery(delivery_reference="Casa verde")

    order.update_details(LATER, delivery=("  911  222 ", None, None))
    assert (order.customer_phone, order.delivery_address, order.delivery_reference) == (
        "911 222",
        "Jr. Pizarro 450",
        "Casa verde",
    )
    order.update_details(LATER, delivery=(None, " Av.  Grau 12 ", None))
    assert order.delivery_address == "Av. Grau 12"
    assert order.customer_phone == "911 222"
    order.update_details(LATER, delivery=(None, None, " portón  negro "))
    assert order.delivery_reference == "portón negro"
    assert order.delivery_address == "Av. Grau 12"
    assert order.updated_at == LATER


@pytest.mark.parametrize(
    ("delivery", "message"),
    [
        (("9" * 21, None, None), "El teléfono admite 20 caracteres como máximo."),
        ((None, "D" * 201, None), "La dirección admite 200 caracteres como máximo."),
        ((None, None, "R" * 151), "La referencia admite 150 caracteres como máximo."),
        (("", None, None), "Un delivery necesita un teléfono para coordinar la entrega."),
        ((None, " ", None), "Un delivery necesita la dirección de entrega."),
    ],
)
def test_invalid_delivery_is_rejected(
    delivery: tuple[str | None, str | None, str | None], message: str
) -> None:
    order = _delivery()

    with pytest.raises(InvalidOrderError) as error:
        order.update_details(LATER, delivery=delivery)
    assert _message(error) == message


def test_delivery_never_ends_up_without_a_name() -> None:
    order = _delivery()

    with pytest.raises(InvalidOrderError) as error:
        order.update_details(LATER, customer_name="   ")
    assert _message(error) == "Un delivery necesita el nombre del cliente."


def test_table_order_can_have_no_customer() -> None:
    order = _order(customer_name="Ana")

    order.update_details(LATER, customer_name="", delivery=("", "", ""))

    assert (order.customer_name, order.customer_phone, order.delivery_address) == ("", "", "")


def test_too_long_note_is_not_saved() -> None:
    order = _order(notes="sin ají")

    with pytest.raises(InvalidOrderError) as error:
        order.update_details(LATER, notes="N" * 301)
    assert _message(error) == "La nota del pedido admite 300 caracteres."
    assert order.notes == "sin ají"
    assert order.updated_at != LATER


def test_closed_order_is_not_edited() -> None:
    order = _order(_item(item_id=1))
    order.cancel("Se fueron", NOW)

    with pytest.raises(InvalidTransitionError) as error:
        order.update_details(LATER, notes="x")
    assert _message(error) == "Un pedido «Cancelado» no se puede editar."


# -- Platos: agregar, cambiar y quitar ---------------------------------------------------


def test_adding_an_empty_list_is_not_allowed() -> None:
    order = _order()

    with pytest.raises(InvalidOrderError) as error:
        order.add_items([], NOW)
    assert _message(error) == "No hay platos para agregar."


def test_adding_dishes_moves_the_update_date() -> None:
    order = _order()

    order.add_items([_item(item_id=1)], LATER)

    assert order.updated_at == LATER
    assert order.item_count == 1


def test_changing_a_dish_touches_only_what_was_sent() -> None:
    order = _order(_item(item_id=1, quantity=2, notes="sin sal"))

    order.change_item(1, LATER, notes="  bien  cocido ")
    assert [(i.quantity, i.notes) for i in order.items] == [(2, "bien cocido")]
    changed = order.change_item(1, LATER, quantity=5)
    assert (changed.quantity, changed.notes) == (5, "bien cocido")
    assert order.updated_at == LATER


def test_changing_or_removing_a_missing_dish_says_which() -> None:
    order = _order(_item(item_id=1))

    with pytest.raises(OrderItemNotFoundError) as change_error:
        order.change_item(99, LATER, quantity=2)
    with pytest.raises(OrderItemNotFoundError) as remove_error:
        order.remove_item(98, LATER)
    assert _message(change_error) == "El pedido no tiene el ítem 99."
    assert change_error.value.item_id == 99
    assert remove_error.value.item_id == 98
    assert order.updated_at != LATER


def test_with_kitchen_in_progress_nothing_is_removed_or_changed() -> None:
    order = _order(_item(item_id=1))
    order.send_to_kitchen(NOW)

    with pytest.raises(InvalidTransitionError) as change_error:
        order.change_item(1, LATER, quantity=2)
    with pytest.raises(InvalidTransitionError) as remove_error:
        order.remove_item(1, LATER)
    assert _message(change_error) == "Un pedido «En cocina» no se puede cambiar sus platos."
    assert _message(remove_error) == "Un pedido «En cocina» no se puede quitarle platos."
    assert change_error.value.status_label == "En cocina"
    assert change_error.value.action == "cambiar sus platos"


def test_removing_a_dish_returns_the_removed_one() -> None:
    lomo, chicha = _item(item_id=1), _item("5.00", item_id=2)
    order = _order(lomo, chicha)

    removed = order.remove_item(2, LATER)

    assert removed is chicha
    assert order.items == [lomo]
    assert order.updated_at == LATER


def test_messages_for_each_invalid_transition() -> None:
    order = _order(_item(item_id=1))

    with pytest.raises(InvalidTransitionError) as ready_error:
        order.mark_ready(NOW)
    with pytest.raises(InvalidTransitionError) as served:
        order.mark_served(NOW)
    with pytest.raises(InvalidTransitionError) as charge_error:
        order.charge(PaymentMethod.CASH, NOW)
    with pytest.raises(InvalidOrderError) as empty:
        _order().send_to_kitchen(NOW)

    assert _message(ready_error) == "Un pedido «Abierto» no se puede marcar como listo."
    assert _message(served) == "Un pedido «Abierto» no se puede marcar como servido."
    assert _message(charge_error) == "Un pedido «Abierto» no se puede cobrar."
    assert _message(empty) == "Un pedido sin platos no se envía a cocina."
    order.send_to_kitchen(LATER)
    with pytest.raises(InvalidTransitionError) as resend_error:
        order.send_to_kitchen(LATER)
    assert _message(resend_error) == "Un pedido «En cocina» no se puede enviar a cocina."
    assert order.status_changed_at == LATER


def test_no_dishes_are_added_to_a_paid_order() -> None:
    order = _served()
    order.charge(PaymentMethod.CARD, NOW)

    with pytest.raises(InvalidTransitionError) as error:
        order.add_items([_item()], NOW)
    assert _message(error) == "Un pedido «Pagado» no se puede agregar platos."


# -- Cuentas y pagos ------------------------------------------------------------------------


def test_order_totals_with_complimentary_item_and_discount() -> None:
    order = _served(_item("20.00", 2, item_id=1), _item("10.00", 1, item_id=2))
    order.grant_courtesy(2, "  Invita  la casa ", NOW)
    order.apply_discount(D("10"), "Frecuente", 5, NOW, limit=None)

    assert order.subtotal == D("50.00")
    assert order.courtesy_amount == D("10.00")
    assert order.discount_amount == D("4.00")
    assert order.total == D("36.00")
    assert order.balance == D("36.00")
    assert order.items[1].courtesy_reason == "Invita la casa"
    assert order.items[1].due == 0


def test_without_payments_there_is_no_method_change_or_received_amount() -> None:
    order = _served()

    assert order.payment_method is None
    assert not order.is_mixed_payment
    assert order.amount_received is None
    assert order.change is None
    assert order.paid_amount == D("0.00")
    assert order.tips == D("0.00")
    assert order.paid_item_ids == frozenset()


def test_with_two_payments_method_is_mixed_and_there_is_no_single_change() -> None:
    order = _served(_item("30.00", item_id=1))
    order.add_payment(
        PaymentMethod.CASH,
        NOW,
        received_by=7,
        amount=D("10"),
        amount_received=D("20"),
        expected_balance=D("30.00"),
    )
    order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, tip=D("2"))

    assert order.payment_method is None
    assert order.is_mixed_payment
    assert order.amount_received is None
    assert order.change is None
    assert order.paid_amount == D("30.00")
    assert order.tips == D("2.00")
    assert order.status is OrderStatus.PAID
    assert order.paid_at == NOW


def test_two_payments_with_same_method_are_not_mixed() -> None:
    order = _served(_item("30.00", item_id=1))
    order.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )
    order.add_payment(PaymentMethod.YAPE, NOW, received_by=7)

    assert order.payment_method is PaymentMethod.YAPE
    assert not order.is_mixed_payment


def test_cash_tip_counts_as_received_and_not_as_sale() -> None:
    order = _served(_item("28.00", item_id=1))

    payment = order.charge(
        PaymentMethod.CASH,
        NOW,
        amount_received=D("40"),
        received_by=9,
        tip=D("2.50"),
        cash_session_id=4,
    )

    assert (payment.amount, payment.tip, payment.amount_received) == (
        D("28.00"),
        D("2.50"),
        D("40.00"),
    )
    assert payment.change == D("9.50")
    assert (payment.received_by, payment.cash_session_id, payment.created_at) == (9, 4, NOW)
    assert order.updated_at == NOW


def test_cash_without_received_amount_means_exact_with_tip() -> None:
    order = _served(_item("28.00", item_id=1))

    payment = order.charge(PaymentMethod.CASH, NOW, tip=D("2"))

    assert payment.amount_received == D("30.00")
    assert payment.change == D("0.00")


def test_cash_must_also_cover_the_tip() -> None:
    order = _served(_item("28.00", item_id=1))

    with pytest.raises(InvalidOrderError) as error:
        order.charge(PaymentMethod.CASH, NOW, amount_received=D("29.99"), tip=D("2"))
    assert _message(error) == "El monto recibido (S/ 29.99) no cubre lo que se cobra (S/ 30.00)."
    order.charge(PaymentMethod.CASH, NOW, amount_received=D("30"), tip=D("2"))
    assert order.change == D("0.00")


def test_tip_is_not_negative_nor_has_more_than_two_decimals() -> None:
    order = _served()

    with pytest.raises(InvalidOrderError) as negative_error:
        order.charge(PaymentMethod.CARD, NOW, tip=D("-0.01"))
    with pytest.raises(InvalidOrderError) as decimals_error:
        order.charge(PaymentMethod.CARD, NOW, tip=D("0.001"))
    with pytest.raises(InvalidOrderError) as received_error:
        order.charge(PaymentMethod.CASH, NOW, amount_received=D("50.001"))
    assert _message(negative_error) == "La propina no puede ser negativa."
    assert _message(decimals_error) == "La propina admite como máximo dos decimales."
    assert _message(received_error) == "El monto recibido admite como máximo dos decimales."
    assert order.payments == []


def test_card_payment_records_no_received_amount() -> None:
    order = _served()

    with pytest.raises(InvalidOrderError) as error:
        order.charge(PaymentMethod.CARD, NOW, amount_received=D("28"))
    assert _message(error) == "El monto recibido solo se anota en pagos en efectivo."
    payment = order.charge(PaymentMethod.CARD, NOW, tip=D("1"))
    assert (payment.amount_received, payment.change) == (None, None)


def test_change_for_cash_payment_without_amount_does_not_exist() -> None:
    payment = Payment(PaymentMethod.CASH, D("10.00"), received_by=1, amount_received=None)

    assert payment.change is None
    assert Payment(PaymentMethod.YAPE, D("10.00"), 1, amount_received=D("20")).change is None


def test_payment_by_amount_validates_amount_and_balance() -> None:
    order = _served(_item("30.00", item_id=1))
    balance = order.balance

    with pytest.raises(InvalidOrderError) as no_balance_error:
        order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"))
    with pytest.raises(InvalidOrderError) as zero_error:
        order.add_payment(
            PaymentMethod.YAPE, NOW, received_by=7, amount=D("0"), expected_balance=balance
        )
    with pytest.raises(InvalidOrderError) as excess_error:
        order.add_payment(
            PaymentMethod.YAPE, NOW, received_by=7, amount=D("30.01"), expected_balance=balance
        )
    with pytest.raises(InvalidOrderError) as decimals_error:
        order.add_payment(
            PaymentMethod.YAPE, NOW, received_by=7, amount=D("1.005"), expected_balance=balance
        )
    with pytest.raises(InvalidOrderError) as both_error:
        order.add_payment(
            PaymentMethod.YAPE,
            NOW,
            received_by=7,
            amount=D("10"),
            item_ids=[1],
            expected_balance=balance,
        )

    assert _message(no_balance_error) == "Un pago por monto necesita el saldo que se vio al cobrar."
    assert _message(zero_error) == "El monto de un pago tiene que ser mayor que cero."
    assert _message(excess_error) == "El monto (S/ 30.01) pasa lo que falta pagar (S/ 30.00)."
    assert _message(decimals_error) == "El monto admite como máximo dos decimales."
    assert _message(both_error) == "Un pago va por monto o por platos, no por las dos cosas."
    assert order.payments == []


def test_payment_for_exact_balance_closes_the_bill() -> None:
    order = _served(_item("30.00", item_id=1))

    payment = order.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("30"), expected_balance=D("30.00")
    )

    assert payment.amount == D("30.00")
    assert order.status is OrderStatus.PAID


def test_partial_payment_leaves_the_order_served() -> None:
    order = _served(_item("30.00", item_id=1))

    order.add_payment(
        PaymentMethod.YAPE, LATER, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )

    assert order.status is OrderStatus.SERVED
    assert order.paid_at is None
    assert order.balance == D("20.00")
    assert order.updated_at == LATER


def test_changed_balance_reports_the_real_balance() -> None:
    order = _served(_item("30.00", item_id=1))

    with pytest.raises(BalanceChangedError) as error:
        order.add_payment(PaymentMethod.CASH, NOW, received_by=7, expected_balance=D("29.00"))
    assert error.value.balance == D("30.00")
    assert (
        _message(error) == "La cuenta cambió mientras se cobraba: ahora faltan S/ 30.00. Revísala."
    )


def test_paying_by_dishes_charges_their_share_and_remembers_who_paid_what() -> None:
    order = _served(_item("20.00", item_id=1), _item("15.00", item_id=2), _item("5.00", item_id=3))

    payment = order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[2, 2, 3])

    assert payment.amount == D("20.00")
    assert payment.item_ids == (2, 3)
    assert order.paid_item_ids == frozenset({2, 3})
    assert order.balance == D("20.00")
    with pytest.raises(InvalidOrderError) as repeated:
        order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[3])
    with pytest.raises(OrderItemNotFoundError):
        order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[99])
    assert _message(repeated) == "Lomo saltado ya se pagó."


def test_dishes_exceeding_the_balance_are_not_charged() -> None:
    order = _served(_item("20.00", item_id=1), _item("20.00", item_id=2))
    order.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("25"), expected_balance=D("40.00")
    )

    with pytest.raises(InvalidOrderError) as error:
        order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[1])
    assert _message(error) == "Esos platos (S/ 20.00) pasan lo que falta pagar (S/ 15.00)."


def test_dishes_equal_to_the_balance_are_charged() -> None:
    order = _served(_item("20.00", item_id=1), _item("20.00", item_id=2), _item("5", item_id=3))
    order.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("25"), expected_balance=D("45.00")
    )

    payment = order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[1])

    assert payment.amount == D("20.00")
    assert order.balance == D("0.00")
    assert order.status is OrderStatus.PAID


# -- Descuentos y cortesías ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("percentage", "message"),
    [
        ("-0.01", "El descuento va de 0 a 100 %."),
        ("100.01", "El descuento va de 0 a 100 %."),
        ("10.005", "El descuento admite como máximo dos decimales."),
    ],
)
def test_out_of_range_discount_is_rejected(percentage: str, message: str) -> None:
    order = _served()

    with pytest.raises(InvalidOrderError) as error:
        order.apply_discount(D(percentage), "Frecuente", 5, NOW, limit=None)
    assert _message(error) == message
    assert order.discount_percent == 0


def test_hundred_percent_discount_leaves_the_bill_at_zero() -> None:
    order = _served(_item("28.00", item_id=1))

    order.apply_discount(D("100"), "Cumpleaños", 5, LATER, limit=None)

    assert order.total == D("0.00")
    assert (order.discount_percent, order.discount_reason, order.discounted_by) == (
        D("100.00"),
        "Cumpleaños",
        5,
    )
    assert order.updated_at == LATER


def test_discount_exactly_at_the_limit_is_applied() -> None:
    order = _served()

    order.apply_discount(D("10"), "Amigo", 7, NOW, limit=D("10"))

    assert order.discount_percent == D("10.00")


def test_discount_limit_is_in_the_message() -> None:
    order = _served()

    with pytest.raises(DiscountNotAllowedError) as error:
        order.apply_discount(D("10.01"), "Amigo", 7, NOW, limit=D("10"))
    assert error.value.limit == D("10")
    assert _message(error) == (
        "Tu descuento máximo es 10 %. Uno mayor, o una cortesía, lo aplica el encargado."
    )


def test_zero_discount_removes_it_without_asking_for_a_reason() -> None:
    order = _served()
    order.apply_discount(D("15"), "Frecuente", 5, NOW, limit=None)

    order.apply_discount(D("0"), "", 5, LATER, limit=D("5"))

    assert (order.discount_percent, order.discount_reason, order.discounted_by) == (
        D("0.00"),
        "",
        None,
    )


def test_discount_requires_a_reason_with_a_length_limit() -> None:
    order = _served()

    with pytest.raises(InvalidOrderError) as no_reason_error:
        order.apply_discount(D("5"), "   ", 5, NOW, limit=None)
    with pytest.raises(InvalidOrderError) as long_error:
        order.apply_discount(D("5"), "m" * 201, 5, NOW, limit=None)
    assert _message(no_reason_error) == "El descuento exige indicar el motivo."
    assert _message(long_error) == "El motivo admite 200 caracteres como máximo."
    assert validate_adjustment_reason("m" * 200, "X") == "m" * 200


def test_complimentary_item_and_its_reversal() -> None:
    order = _order(_item(item_id=1))

    complimentary = order.grant_courtesy(1, "  Error  de cocina ", LATER)
    assert (complimentary.is_courtesy, complimentary.courtesy_reason) == (True, "Error de cocina")
    assert order.updated_at == LATER
    assert order.total == D("0.00")

    later = LATER + timedelta(minutes=1)
    reverted = order.revoke_courtesy(1, later)
    assert (reverted.is_courtesy, reverted.courtesy_reason) == (False, "")
    assert order.updated_at == later
    assert order.total == D("28.00")


def test_complimentary_item_without_reason_is_not_recorded() -> None:
    order = _order(_item(item_id=1))

    with pytest.raises(InvalidOrderError) as error:
        order.grant_courtesy(1, "  ", LATER)
    assert _message(error) == "La cortesía exige indicar el motivo."
    with pytest.raises(OrderItemNotFoundError):
        order.grant_courtesy(2, "Invita", LATER)
    with pytest.raises(OrderItemNotFoundError):
        order.revoke_courtesy(2, LATER)


def test_with_payments_or_closed_there_are_no_complimentary_items_or_discounts() -> None:
    order = _served(_item("30.00", item_id=1))
    order.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )

    with pytest.raises(OrderHasPaymentsError) as comp_error:
        order.grant_courtesy(1, "Invita", LATER)
    with pytest.raises(OrderHasPaymentsError) as remove_error:
        order.revoke_courtesy(1, LATER)
    assert _message(comp_error) == "Un pedido con pagos registrados no se puede invitarle un plato."
    assert (
        _message(remove_error)
        == "Un pedido con pagos registrados no se puede quitarle una cortesía."
    )
    assert comp_error.value.action == "invitarle un plato"

    closed = _order(_item(item_id=1))
    closed.cancel("Se fueron", NOW)
    with pytest.raises(InvalidTransitionError) as discount_error:
        closed.apply_discount(D("5"), "x", 1, NOW, limit=None)
    assert _message(discount_error) == "Un pedido «Cancelado» no se puede aplicarle un descuento."


# -- Mesas: mover y unir ---------------------------------------------------------------------


def test_moving_table_changes_the_table_and_the_date() -> None:
    order = _order(_item(item_id=1))

    order.move_to_table(8, LATER)

    assert order.table_id == 8
    assert order.updated_at == LATER
    assert order.status_changed_at != LATER


def test_moving_to_same_table_or_an_order_without_table_is_rejected() -> None:
    order = _order(_item(item_id=1))
    takeaway = _order(_item(item_id=1), type=OrderType.TAKEAWAY, table_id=None)

    with pytest.raises(InvalidOrderError) as same_table_error:
        order.move_to_table(3, LATER)
    with pytest.raises(InvalidOrderError) as no_table_error:
        takeaway.move_to_table(3, LATER)
    assert _message(same_table_error) == "El pedido ya está en esa mesa."
    assert _message(no_table_error) == "Un pedido para llevar o delivery no ocupa mesa."
    assert takeaway.table_id is None
    assert order.updated_at != LATER


def test_closed_order_does_not_change_table() -> None:
    order = _served()
    order.charge(PaymentMethod.CARD, NOW)

    with pytest.raises(InvalidTransitionError) as error:
        order.move_to_table(8, LATER)
    assert _message(error) == "Un pedido «Pagado» no se puede cambiar de mesa."
    assert order.table_id == 3


def test_merging_tables_moves_dishes_joins_notes_and_takes_the_least_advanced_status() -> None:
    kept = _served(_item(item_id=1), id=10, notes="sin ají")
    merged = _order(_item("5.00", item_id=2), id=11, number=15, table_id=4, notes="con hielo")
    merged.send_to_kitchen(NOW)

    kept.absorb(merged, LATER)

    assert [i.id for i in kept.items] == [1, 2]
    assert merged.items == []
    assert kept.status is OrderStatus.IN_KITCHEN
    assert kept.status_changed_at == LATER
    assert kept.notes == "sin ají · con hielo"
    assert kept.updated_at == LATER
    assert (merged.status, merged.merged_into_id, merged.cancelled_at) == (
        OrderStatus.CANCELLED,
        10,
        LATER,
    )
    assert merged.cancel_reason == "Unido al pedido #14"


def test_merging_with_a_more_advanced_table_keeps_status_and_note() -> None:
    kept = _order(_item(item_id=1), id=10, notes="sin ají")
    merged = _served(_item(item_id=2), id=11, table_id=4, notes="sin ají")

    kept.absorb(merged, LATER)

    assert kept.status is OrderStatus.OPEN
    assert kept.status_changed_at != LATER
    assert kept.notes == "sin ají"


def test_merging_joins_notes_without_exceeding_the_maximum_length() -> None:
    kept = _order(_item(item_id=1), id=10, notes="a" * 290)
    merged = _order(_item(item_id=2), id=11, table_id=4, notes="b" * 20)

    kept.absorb(merged, LATER)

    assert len(kept.notes) == MAX_ORDER_NOTES_LENGTH
    assert kept.notes.startswith("a" * 290 + " · ")


def test_merging_with_a_table_without_note_keeps_its_own() -> None:
    kept = _order(_item(item_id=1), id=10)
    merged = _order(_item(item_id=2), id=11, table_id=4, notes="con hielo")

    kept.absorb(merged, LATER)

    assert kept.notes == "con hielo"


def test_cannot_merge_with_itself_nor_closed_nor_with_payments_nor_takeaway() -> None:
    base = _order(_item(item_id=1), id=10)
    closed = _order(_item(item_id=2), id=11, table_id=4)
    closed.cancel("Se fueron", NOW)
    with_payment = _served(_item("30", item_id=3), id=12, table_id=5)
    with_payment.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )
    takeaway_order = _order(_item(item_id=4), id=13, type=OrderType.TAKEAWAY, table_id=None)

    with pytest.raises(InvalidOrderError) as self_error:
        base.absorb(base, LATER)
    with pytest.raises(InvalidTransitionError) as closed_error:
        base.absorb(closed, LATER)
    with pytest.raises(OrderHasPaymentsError) as payments_error:
        base.absorb(with_payment, LATER)
    with pytest.raises(InvalidOrderError) as takeaway_error:
        base.absorb(takeaway_order, LATER)
    with pytest.raises(InvalidOrderError):
        takeaway_order.absorb(base, LATER)

    assert _message(self_error) == "No se puede unir un pedido consigo mismo."
    assert _message(closed_error) == "Un pedido «Cancelado» no se puede unir."
    assert _message(payments_error) == "Un pedido con pagos registrados no se puede unir."
    assert _message(takeaway_error) == "Solo se unen pedidos en mesa."
    assert [i.id for i in base.items] == [1]


# -- Cancelar ---------------------------------------------------------------------------------


def test_cancellation_reason_has_a_limit() -> None:
    order = _order(_item(item_id=1))

    with pytest.raises(InvalidOrderError) as long_error:
        order.cancel("m" * 301, NOW)
    with pytest.raises(InvalidOrderError) as empty:
        order.cancel(" \n ", NOW)
    assert _message(long_error) == "El motivo admite 300 caracteres como máximo."
    assert _message(empty) == "Cancelar un pedido exige indicar el motivo."
    order.cancel("m" * 300, LATER)
    assert order.status_changed_at == LATER


def test_order_with_payments_or_already_closed_is_not_cancelled() -> None:
    order = _served(_item("30", item_id=1))
    order.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )
    cancelled = _order(_item(item_id=1))
    cancelled.cancel("Se fueron", NOW)

    with pytest.raises(OrderHasPaymentsError) as payments_error:
        order.cancel("Error", NOW)
    with pytest.raises(InvalidTransitionError) as again_error:
        cancelled.cancel("Otra vez", NOW)
    assert _message(payments_error) == "Un pedido con pagos registrados no se puede cancelar."
    assert _message(again_error) == "Un pedido «Cancelado» no se puede cancelar."
    assert cancelled.cancel_reason == "Se fueron"


# -- Mensajes de los errores ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: TableNotFoundError(3), "No existe la mesa 3."),
        (lambda: TableLabelTakenError("Barra"), "Ya existe una mesa llamada 'Barra'."),
        (lambda: TableInactiveError("Barra"), "La mesa Barra está desactivada."),
        (lambda: TableOccupiedError("Barra", 9), "La mesa Barra ya tiene un pedido activo."),
        (
            InvalidTableOrderingError,
            "El nuevo orden tiene que nombrar exactamente una vez a cada mesa del local.",
        ),
        (lambda: OrderNotFoundError(4), "No existe el pedido 4."),
        (lambda: DishNotFoundError(5), "No existe el plato 5."),
        (lambda: DishUnavailableError("Ceviche"), "Ceviche no está disponible hoy."),
        (
            OrderNumberTakenError,
            "Otro pedido tomó el mismo número a la vez. Vuelve a intentarlo.",
        ),
        (lambda: CustomerNotFoundError(6), "No existe el cliente 6."),
        (
            lambda: NotYourOrderError("cobrarlo"),
            "Solo el mesero que tomó el pedido o el encargado pueden cobrarlo.",
        ),
        (
            CashRegisterClosedError,
            "La caja está cerrada. El encargado tiene que abrirla para cobrar.",
        ),
        (
            CashRegisterAlreadyOpenError,
            "Ya hay una caja abierta. Ciérrala antes de abrir otra.",
        ),
        (CashSessionNotFoundError, "No hay una caja abierta."),
        (lambda: CashSessionNotFoundError(8), "No existe el turno de caja 8."),
        (lambda: InvalidTableError("Mesa rara"), "Mesa rara"),
        (lambda: InvalidCashSessionError("Caja rara"), "Caja rara"),
    ],
)
def test_each_error_explains_what_happened(factory: Callable[[], Exception], message: str) -> None:
    assert str(factory()) == message


def test_errors_keep_their_data() -> None:
    assert TableNotFoundError(3).table_id == 3
    assert TableLabelTakenError("Barra").label == "Barra"
    assert TableInactiveError("Barra").label == "Barra"
    occupied = TableOccupiedError("Barra", 9)
    assert (occupied.label, occupied.order_id) == ("Barra", 9)
    assert OrderNotFoundError(4).order_id == 4
    assert DishNotFoundError(5).menu_item_id == 5
    assert DishUnavailableError("Ceviche").name == "Ceviche"
    assert CustomerNotFoundError(6).customer_id == 6
    assert NotYourOrderError("cobrarlo").action == "cobrarlo"
    assert CashSessionNotFoundError().session_id is None
    assert CashSessionNotFoundError(8).session_id == 8
    assert InvalidTableError("x").reason == "x"
    assert InvalidCashSessionError("y").reason == "y"
    assert InvalidOrderError("z").reason == "z"


# -- Caja -----------------------------------------------------------------------------------------


def test_new_cash_session_is_open_and_without_difference() -> None:
    cash_session = CashSession(1, 2, D("100"), opening_notes="  sencillo  ")

    assert cash_session.is_open
    assert cash_session.difference is None
    assert str(cash_session.opening_amount) == "100.00"
    assert cash_session.opening_notes == "sencillo"


def test_closing_the_cash_session_sets_the_count_and_the_difference() -> None:
    cash_session = CashSession(1, 2, D("100"))

    cash_session.close(D("149.5"), D("150.004"), actor_id=3, now=LATER, notes="  faltó sencillo ")

    assert not cash_session.is_open
    assert (cash_session.counted_cash, cash_session.expected_cash) == (D("149.50"), D("150.00"))
    assert cash_session.difference == D("-0.50")
    assert (cash_session.closed_by, cash_session.closed_at, cash_session.closing_notes) == (
        3,
        LATER,
        "faltó sencillo",
    )
    with pytest.raises(InvalidTransitionError) as error:
        cash_session.close(D("1"), D("1"), actor_id=3, now=LATER, notes="")
    assert _message(error) == "Un pedido «Cerrada» no se puede cerrar otra vez."


def test_difference_needs_counted_and_expected() -> None:
    cash_session = CashSession(1, 2, D("100"), counted_cash=D("10"))

    assert cash_session.difference is None
    cash_session.expected_cash = D("12")
    assert cash_session.difference == D("-2.00")


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("-0.01", "El monto inicial no puede ser negativo."),
        ("10.005", "El monto inicial admite como máximo dos decimales."),
        ("100000000.00", "El monto inicial es demasiado grande."),
    ],
)
def test_invalid_cash_session_amount_is_rejected(amount: str, message: str) -> None:
    with pytest.raises(InvalidCashSessionError) as error:
        CashSession(1, 2, D(amount))
    assert _message(error) == message


def test_cash_session_boundary_amounts_are_accepted() -> None:
    assert CashSession(1, 2, D("0")).opening_amount == D("0.00")
    assert validate_cash_amount(MAX_CASH_AMOUNT, "El monto") == MAX_CASH_AMOUNT


def test_cash_session_note_has_a_limit() -> None:
    assert len(CashSession(1, 2, D("0"), opening_notes="n" * 300).opening_notes) == 300
    with pytest.raises(InvalidCashSessionError) as error:
        CashSession(1, 2, D("0"), opening_notes="n" * 301)
    assert _message(error) == "La nota de la caja admite 300 caracteres como máximo."
    cash_session = CashSession(1, 2, D("0"))
    with pytest.raises(InvalidCashSessionError):
        cash_session.close(D("1"), D("1"), actor_id=1, now=NOW, notes="n" * 301)
    assert cash_session.is_open


def _payment(
    order: int, waiter: int, method: PaymentMethod, amount: str, tip: str = "0"
) -> CashPayment:
    return CashPayment(order, order, waiter, 99, method, D(amount), D(tip), NOW)


def test_count_sums_by_method_and_by_waiter() -> None:
    cash_session = CashSession(1, 2, D("100"))
    payments = [
        _payment(1, 7, PaymentMethod.CASH, "30.00", "2.00"),
        _payment(1, 7, PaymentMethod.YAPE, "10.00", "1.00"),
        _payment(2, 8, PaymentMethod.CASH, "20.00", "5.00"),
        _payment(3, 9, PaymentMethod.CARD, "50.00"),
    ]
    adjustments = [
        CashOrderAdjustment(1, D("3.00"), D("10.00")),
        CashOrderAdjustment(2, D("0"), D("0")),
    ]

    summary = summarize_cash(cash_session, iter(payments), iter(adjustments), {7: "Ana", 8: "Beto"})

    assert [(m.method, m.payments, m.amount, m.tips) for m in summary.by_method] == [
        (PaymentMethod.CASH, 2, D("50.00"), D("7.00")),
        (PaymentMethod.YAPE, 1, D("10.00"), D("1.00")),
        (PaymentMethod.CARD, 1, D("50.00"), D("0.00")),
    ]
    # Primero quien más propina recibió; a igual propina, quien más vendió.
    assert [(w.waiter_id, w.name, w.payments, w.sales, w.tips) for w in summary.by_waiter] == [
        (8, "Beto", 1, D("20.00"), D("5.00")),
        (7, "Ana", 2, D("40.00"), D("3.00")),
        (9, "", 1, D("50.00"), D("0.00")),
    ]
    assert summary.opening_amount == D("100.00")
    assert (summary.sales, summary.tips) == (D("110.00"), D("8.00"))
    # Inicial 100 + efectivo 50 + propinas en efectivo 7.
    assert summary.expected_cash == D("157.00")
    assert summary.paid_orders == 3
    assert (summary.discounts, summary.discounted_orders, summary.courtesies) == (
        D("3.00"),
        1,
        D("10.00"),
    )


def test_count_breaks_ties_by_sales_then_by_name() -> None:
    cash_session = CashSession(1, 2, D("0"))
    payments = [
        _payment(1, 1, PaymentMethod.YAPE, "10.00", "1.00"),
        _payment(2, 2, PaymentMethod.YAPE, "30.00", "1.00"),
        _payment(3, 3, PaymentMethod.YAPE, "10.00", "1.00"),
    ]

    summary = summarize_cash(cash_session, payments, [], {1: "Zoila", 2: "Mario", 3: "Ana"})

    assert [w.name for w in summary.by_waiter] == ["Mario", "Ana", "Zoila"]


def test_shift_without_payments_expects_only_the_initial_amount() -> None:
    summary = summarize_cash(CashSession(1, 2, D("80")), [], [], {})

    assert summary.by_method == ()
    assert summary.by_waiter == ()
    assert (summary.sales, summary.tips, summary.expected_cash) == (
        D("0.00"),
        D("0.00"),
        D("80.00"),
    )
    assert (summary.paid_orders, summary.discounted_orders) == (0, 0)


# -- Mesas ---------------------------------------------------------------------------------------


def test_table_cleans_its_name_and_validates_the_length() -> None:
    table = DiningTable(1, "  Terraza   2 ")

    assert table.label == "Terraza 2"
    table.relabel("B" * MAX_LABEL_LENGTH)
    assert len(table.label) == MAX_LABEL_LENGTH
    with pytest.raises(InvalidTableError) as long_error:
        table.relabel("B" * (MAX_LABEL_LENGTH + 1))
    with pytest.raises(InvalidTableError) as empty:
        DiningTable(1, "   ")
    assert _message(long_error) == "El nombre de la mesa no puede pasar de 30 caracteres."
    assert _message(empty) == "El nombre de la mesa no puede quedar vacío."


def test_reordering_tables_requires_an_exact_permutation() -> None:
    tables = [DiningTable(1, str(i), id=i) for i in (1, 2, 3)]

    reordered = reorder_tables(tables, [3, 1, 2])

    assert [(m.id, m.position) for m in reordered] == [(3, 0), (1, 1), (2, 2)]
    for ids in ([1, 2], [1, 2, 3, 4], [1, 1, 2]):
        with pytest.raises(InvalidTableOrderingError):
            reorder_tables(tables, ids)


# -- Opciones elegidas al pedir -------------------------------------------------------------------


def _groups() -> list[DishOptionGroup]:
    return [
        DishOptionGroup(
            "Tamaño",
            (DishOption("Personal", D("0")), DishOption("Familiar", D("10"))),
            min_choices=1,
        ),
        DishOptionGroup(
            "Extras",
            (DishOption("Queso", D("2")), DishOption("Huevo", D("1.5"))),
            max_choices=2,
        ),
    ]


def test_chosen_options_are_frozen_with_their_price() -> None:
    chosen = choose_modifiers(
        "Pizza", _groups(), [("tamaño", "FAMILIAR"), ("Extras", "queso"), ("extras", "Huevo")]
    )

    assert [(c.group, c.option, c.price) for c in chosen] == [
        ("Tamaño", "Familiar", D("10")),
        ("Extras", "Queso", D("2")),
        ("Extras", "Huevo", D("1.5")),
    ]


@pytest.mark.parametrize(
    ("chosen", "message"),
    [
        ([], "Elige tamaño para Pizza."),
        ([("Salsa", "BBQ")], "Pizza no tiene la opción «Salsa»."),
        ([("Tamaño", "Mediana")], "«Mediana» no es una opción de Tamaño."),
        ([("Tamaño", "Personal"), ("tamaño", "personal")], "«Personal» se eligió dos veces."),
        (
            [("Tamaño", "Personal"), ("Tamaño", "Familiar")],
            "En tamaño se eligen como máximo 1.",
        ),
        ([("Tamaño", "Personal")] * (MAX_CHOSEN + 1), "Demasiadas opciones para Pizza."),
    ],
)
def test_invalid_choice_is_rejected(chosen: list[tuple[str, str]], message: str) -> None:
    with pytest.raises(InvalidOrderError) as error:
        choose_modifiers("Pizza", _groups(), chosen)
    assert _message(error) == message


def test_without_groups_there_is_nothing_to_choose() -> None:
    assert choose_modifiers("Chicha", [], []) == ()
