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
    BalanceChanged,
    CashRegisterAlreadyOpen,
    CashRegisterClosed,
    CashSessionNotFound,
    CustomerNotFound,
    DiscountNotAllowed,
    DishNotFound,
    DishUnavailable,
    InvalidCashSession,
    InvalidOrder,
    InvalidTable,
    InvalidTableOrdering,
    InvalidTransition,
    NotYourOrder,
    OrderHasPayments,
    OrderItemNotFound,
    OrderNotFound,
    OrderNumberTaken,
    TableInactive,
    TableLabelTaken,
    TableNotFound,
    TableOccupied,
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


def _mensaje(error: pytest.ExceptionInfo[BaseException]) -> str:
    return str(error.value)


def _item(price: str = "28.00", quantity: int = 1, item_id: int | None = None, **extra: object):
    campos: dict[str, object] = {"menu_item_id": 1, "name": "Lomo saltado"}
    campos.update(extra)
    return OrderItem(unit_price=D(price), quantity=quantity, id=item_id, **campos)  # type: ignore[arg-type]


def _order(*items: OrderItem, **extra: object) -> Order:
    campos: dict[str, object] = {
        "restaurant_id": 1,
        "number": 14,
        "business_date": date(2026, 9, 25),
        "type": OrderType.DINE_IN,
        "waiter_id": 7,
        "table_id": 3,
        "items": list(items),
    }
    campos.update(extra)
    return Order(**campos)  # type: ignore[arg-type]


def _delivery(**extra: object) -> Order:
    campos: dict[str, object] = {
        "type": OrderType.DELIVERY,
        "table_id": None,
        "customer_name": "Ana",
        "customer_phone": "987654321",
        "delivery_address": "Jr. Pizarro 450",
    }
    campos.update(extra)
    return _order(_item(item_id=1), **campos)


def _served(*items: OrderItem, **extra: object) -> Order:
    order = _order(*(items or (_item(item_id=1),)), **extra)
    order.send_to_kitchen(NOW)
    order.mark_ready(NOW)
    order.mark_served(NOW)
    return order


# -- Rótulos --------------------------------------------------------------------------


def test_rotulos_de_tipos_estados_medios_y_mesas() -> None:
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


def test_el_plato_congela_nombre_y_precio_con_centimos() -> None:
    plato = _item("12.5", 2, name="  Lomo saltado  ", notes="  sin   cebolla ")

    assert plato.name == "Lomo saltado"
    assert str(plato.unit_price) == "12.50"
    assert plato.notes == "sin cebolla"
    assert plato.subtotal == D("25.00")
    assert plato.due == D("25.00")


def test_el_nombre_del_plato_se_recorta_al_maximo() -> None:
    plato = _item(name="L" * (MAX_DISH_NAME_LENGTH + 10))

    assert len(plato.name) == MAX_DISH_NAME_LENGTH


def test_un_plato_de_precio_cero_se_acepta_y_uno_negativo_no() -> None:
    assert _item("0").subtotal == D("0.00")
    with pytest.raises(InvalidOrder) as error:
        _item("-0.01")
    assert _mensaje(error) == "El precio de un plato no puede ser negativo."


@pytest.mark.parametrize("cantidad", [0, -1, MAX_QUANTITY + 1])
def test_la_cantidad_va_de_uno_a_noventa_y_nueve(cantidad: int) -> None:
    with pytest.raises(InvalidOrder) as error:
        _item(quantity=cantidad)
    assert _mensaje(error) == "La cantidad tiene que estar entre 1 y 99."


def test_las_cantidades_limite_se_aceptan() -> None:
    assert _item(quantity=1).quantity == 1
    assert _item(quantity=MAX_QUANTITY).quantity == 99


def test_la_nota_del_plato_tiene_tope() -> None:
    assert len(_item(notes="n" * MAX_ITEM_NOTES_LENGTH).notes) == MAX_ITEM_NOTES_LENGTH
    with pytest.raises(InvalidOrder) as error:
        _item(notes="n" * (MAX_ITEM_NOTES_LENGTH + 1))
    assert _mensaje(error) == "La nota del plato admite 200 caracteres."


def test_una_cortesia_no_se_cobra_pero_suma_al_subtotal() -> None:
    plato = _item("10.00", 2, is_courtesy=True)

    assert plato.subtotal == D("20.00")
    assert plato.due == D("0.00")


# -- Datos del pedido -----------------------------------------------------------------


def test_el_pedido_limpia_sus_textos_y_recorta_el_identificador() -> None:
    pedido = _order(
        customer_name="  Ana   Torres ",
        notes="  sin ají  ",
        client_request_id="  " + "x" * (MAX_CLIENT_REQUEST_ID_LENGTH + 5) + "  ",
    )

    assert pedido.customer_name == "Ana Torres"
    assert pedido.notes == "sin ají"
    assert pedido.client_request_id == "x" * MAX_CLIENT_REQUEST_ID_LENGTH
    assert pedido.status_changed_at == pedido.created_at


def test_sin_identificador_de_reintento_queda_vacio() -> None:
    assert _order().client_request_id is None


@pytest.mark.parametrize(
    ("datos", "mensaje"),
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
def test_un_pedido_invalido_explica_el_motivo(datos: dict[str, object], mensaje: str) -> None:
    with pytest.raises(InvalidOrder) as error:
        _order(**datos)
    assert _mensaje(error) == mensaje


def test_los_largos_maximos_del_pedido_se_aceptan() -> None:
    pedido = _order(
        customer_name="N" * MAX_CUSTOMER_NAME_LENGTH,
        customer_phone="9" * MAX_PHONE_LENGTH,
        delivery_address="D" * MAX_ADDRESS_LENGTH,
        delivery_reference="R" * MAX_REFERENCE_LENGTH,
        notes="N" * MAX_ORDER_NOTES_LENGTH,
    )

    assert len(pedido.customer_name) == 80
    assert len(pedido.customer_phone) == 20
    assert len(pedido.delivery_address) == 200
    assert len(pedido.delivery_reference) == 150
    assert len(pedido.notes) == 300


@pytest.mark.parametrize(
    ("falta", "mensaje"),
    [
        ({"customer_name": " "}, "Un delivery necesita el nombre del cliente."),
        ({"customer_phone": ""}, "Un delivery necesita un teléfono para coordinar la entrega."),
        ({"delivery_address": "  "}, "Un delivery necesita la dirección de entrega."),
    ],
)
def test_un_delivery_necesita_a_quien_y_a_donde(falta: dict[str, str], mensaje: str) -> None:
    with pytest.raises(InvalidOrder) as error:
        _delivery(**falta)
    assert _mensaje(error) == mensaje


def test_un_delivery_completo_se_acepta() -> None:
    pedido = _delivery(delivery_reference=" frente al parque ")

    assert pedido.table_id is None
    assert pedido.delivery_reference == "frente al parque"


def test_actualizar_los_datos_cambia_solo_lo_enviado() -> None:
    pedido = _order(notes="sin ají", customer_name="Ana")

    pedido.update_details(LATER, customer_name="  Luis  ")

    assert (pedido.notes, pedido.customer_name) == ("sin ají", "Luis")
    assert pedido.updated_at == LATER
    pedido.update_details(LATER, notes="  con  hielo ")
    assert (pedido.notes, pedido.customer_name) == ("con  hielo", "Luis")


def test_actualizar_la_entrega_cambia_cada_campo_por_separado() -> None:
    pedido = _delivery(delivery_reference="Casa verde")

    pedido.update_details(LATER, delivery=("  911  222 ", None, None))
    assert (pedido.customer_phone, pedido.delivery_address, pedido.delivery_reference) == (
        "911 222",
        "Jr. Pizarro 450",
        "Casa verde",
    )
    pedido.update_details(LATER, delivery=(None, " Av.  Grau 12 ", None))
    assert pedido.delivery_address == "Av. Grau 12"
    assert pedido.customer_phone == "911 222"
    pedido.update_details(LATER, delivery=(None, None, " portón  negro "))
    assert pedido.delivery_reference == "portón negro"
    assert pedido.delivery_address == "Av. Grau 12"
    assert pedido.updated_at == LATER


@pytest.mark.parametrize(
    ("entrega", "mensaje"),
    [
        (("9" * 21, None, None), "El teléfono admite 20 caracteres como máximo."),
        ((None, "D" * 201, None), "La dirección admite 200 caracteres como máximo."),
        ((None, None, "R" * 151), "La referencia admite 150 caracteres como máximo."),
        (("", None, None), "Un delivery necesita un teléfono para coordinar la entrega."),
        ((None, " ", None), "Un delivery necesita la dirección de entrega."),
    ],
)
def test_una_entrega_invalida_se_rechaza(
    entrega: tuple[str | None, str | None, str | None], mensaje: str
) -> None:
    pedido = _delivery()

    with pytest.raises(InvalidOrder) as error:
        pedido.update_details(LATER, delivery=entrega)
    assert _mensaje(error) == mensaje


def test_un_delivery_no_se_queda_sin_nombre() -> None:
    pedido = _delivery()

    with pytest.raises(InvalidOrder) as error:
        pedido.update_details(LATER, customer_name="   ")
    assert _mensaje(error) == "Un delivery necesita el nombre del cliente."


def test_un_pedido_en_mesa_puede_quedar_sin_cliente() -> None:
    pedido = _order(customer_name="Ana")

    pedido.update_details(LATER, customer_name="", delivery=("", "", ""))

    assert (pedido.customer_name, pedido.customer_phone, pedido.delivery_address) == ("", "", "")


def test_una_nota_demasiado_larga_no_se_guarda() -> None:
    pedido = _order(notes="sin ají")

    with pytest.raises(InvalidOrder) as error:
        pedido.update_details(LATER, notes="N" * 301)
    assert _mensaje(error) == "La nota del pedido admite 300 caracteres."
    assert pedido.notes == "sin ají"
    assert pedido.updated_at != LATER


def test_un_pedido_cerrado_no_se_edita() -> None:
    pedido = _order(_item(item_id=1))
    pedido.cancel("Se fueron", NOW)

    with pytest.raises(InvalidTransition) as error:
        pedido.update_details(LATER, notes="x")
    assert _mensaje(error) == "Un pedido «Cancelado» no se puede editar."


# -- Platos: agregar, cambiar y quitar ---------------------------------------------------


def test_agregar_una_lista_vacia_no_se_permite() -> None:
    pedido = _order()

    with pytest.raises(InvalidOrder) as error:
        pedido.add_items([], NOW)
    assert _mensaje(error) == "No hay platos para agregar."


def test_agregar_platos_mueve_la_fecha_de_actualizacion() -> None:
    pedido = _order()

    pedido.add_items([_item(item_id=1)], LATER)

    assert pedido.updated_at == LATER
    assert pedido.item_count == 1


def test_cambiar_un_plato_toca_solo_lo_enviado() -> None:
    pedido = _order(_item(item_id=1, quantity=2, notes="sin sal"))

    pedido.change_item(1, LATER, notes="  bien  cocido ")
    assert [(i.quantity, i.notes) for i in pedido.items] == [(2, "bien cocido")]
    cambiado = pedido.change_item(1, LATER, quantity=5)
    assert (cambiado.quantity, cambiado.notes) == (5, "bien cocido")
    assert pedido.updated_at == LATER


def test_cambiar_o_quitar_un_plato_que_no_esta_avisa_cual() -> None:
    pedido = _order(_item(item_id=1))

    with pytest.raises(OrderItemNotFound) as cambiar:
        pedido.change_item(99, LATER, quantity=2)
    with pytest.raises(OrderItemNotFound) as quitar:
        pedido.remove_item(98, LATER)
    assert _mensaje(cambiar) == "El pedido no tiene el ítem 99."
    assert cambiar.value.item_id == 99
    assert quitar.value.item_id == 98
    assert pedido.updated_at != LATER


def test_con_la_cocina_en_marcha_no_se_quita_ni_se_cambia() -> None:
    pedido = _order(_item(item_id=1))
    pedido.send_to_kitchen(NOW)

    with pytest.raises(InvalidTransition) as cambiar:
        pedido.change_item(1, LATER, quantity=2)
    with pytest.raises(InvalidTransition) as quitar:
        pedido.remove_item(1, LATER)
    assert _mensaje(cambiar) == "Un pedido «En cocina» no se puede cambiar sus platos."
    assert _mensaje(quitar) == "Un pedido «En cocina» no se puede quitarle platos."
    assert cambiar.value.status_label == "En cocina"
    assert cambiar.value.action == "cambiar sus platos"


def test_quitar_un_plato_devuelve_el_quitado() -> None:
    lomo, chicha = _item(item_id=1), _item("5.00", item_id=2)
    pedido = _order(lomo, chicha)

    quitado = pedido.remove_item(2, LATER)

    assert quitado is chicha
    assert pedido.items == [lomo]
    assert pedido.updated_at == LATER


def test_los_mensajes_de_cada_transicion_invalida() -> None:
    pedido = _order(_item(item_id=1))

    with pytest.raises(InvalidTransition) as listo:
        pedido.mark_ready(NOW)
    with pytest.raises(InvalidTransition) as servido:
        pedido.mark_served(NOW)
    with pytest.raises(InvalidTransition) as cobrar:
        pedido.charge(PaymentMethod.CASH, NOW)
    with pytest.raises(InvalidOrder) as vacio:
        _order().send_to_kitchen(NOW)

    assert _mensaje(listo) == "Un pedido «Abierto» no se puede marcar como listo."
    assert _mensaje(servido) == "Un pedido «Abierto» no se puede marcar como servido."
    assert _mensaje(cobrar) == "Un pedido «Abierto» no se puede cobrar."
    assert _mensaje(vacio) == "Un pedido sin platos no se envía a cocina."
    pedido.send_to_kitchen(LATER)
    with pytest.raises(InvalidTransition) as reenviar:
        pedido.send_to_kitchen(LATER)
    assert _mensaje(reenviar) == "Un pedido «En cocina» no se puede enviar a cocina."
    assert pedido.status_changed_at == LATER


def test_a_un_pedido_pagado_no_se_le_agregan_platos() -> None:
    pedido = _served()
    pedido.charge(PaymentMethod.CARD, NOW)

    with pytest.raises(InvalidTransition) as error:
        pedido.add_items([_item()], NOW)
    assert _mensaje(error) == "Un pedido «Pagado» no se puede agregar platos."


# -- Cuentas y pagos ------------------------------------------------------------------------


def test_las_cuentas_de_un_pedido_con_cortesia_y_descuento() -> None:
    pedido = _served(_item("20.00", 2, item_id=1), _item("10.00", 1, item_id=2))
    pedido.grant_courtesy(2, "  Invita  la casa ", NOW)
    pedido.apply_discount(D("10"), "Frecuente", 5, NOW, limit=None)

    assert pedido.subtotal == D("50.00")
    assert pedido.courtesy_amount == D("10.00")
    assert pedido.discount_amount == D("4.00")
    assert pedido.total == D("36.00")
    assert pedido.balance == D("36.00")
    assert pedido.items[1].courtesy_reason == "Invita la casa"
    assert pedido.items[1].due == 0


def test_sin_pagos_no_hay_medio_ni_vuelto_ni_monto_recibido() -> None:
    pedido = _served()

    assert pedido.payment_method is None
    assert not pedido.is_mixed_payment
    assert pedido.amount_received is None
    assert pedido.change is None
    assert pedido.paid_amount == D("0.00")
    assert pedido.tips == D("0.00")
    assert pedido.paid_item_ids == frozenset()


def test_con_dos_pagos_el_medio_es_mixto_y_no_hay_vuelto_unico() -> None:
    pedido = _served(_item("30.00", item_id=1))
    pedido.add_payment(
        PaymentMethod.CASH,
        NOW,
        received_by=7,
        amount=D("10"),
        amount_received=D("20"),
        expected_balance=D("30.00"),
    )
    pedido.add_payment(PaymentMethod.YAPE, NOW, received_by=7, tip=D("2"))

    assert pedido.payment_method is None
    assert pedido.is_mixed_payment
    assert pedido.amount_received is None
    assert pedido.change is None
    assert pedido.paid_amount == D("30.00")
    assert pedido.tips == D("2.00")
    assert pedido.status is OrderStatus.PAID
    assert pedido.paid_at == NOW


def test_dos_pagos_con_el_mismo_medio_no_son_mixtos() -> None:
    pedido = _served(_item("30.00", item_id=1))
    pedido.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )
    pedido.add_payment(PaymentMethod.YAPE, NOW, received_by=7)

    assert pedido.payment_method is PaymentMethod.YAPE
    assert not pedido.is_mixed_payment


def test_la_propina_en_efectivo_entra_en_lo_recibido_y_no_en_la_venta() -> None:
    pedido = _served(_item("28.00", item_id=1))

    pago = pedido.charge(
        PaymentMethod.CASH,
        NOW,
        amount_received=D("40"),
        received_by=9,
        tip=D("2.50"),
        cash_session_id=4,
    )

    assert (pago.amount, pago.tip, pago.amount_received) == (D("28.00"), D("2.50"), D("40.00"))
    assert pago.change == D("9.50")
    assert (pago.received_by, pago.cash_session_id, pago.created_at) == (9, 4, NOW)
    assert pedido.updated_at == NOW


def test_en_efectivo_sin_monto_recibido_se_entiende_justo_con_propina() -> None:
    pedido = _served(_item("28.00", item_id=1))

    pago = pedido.charge(PaymentMethod.CASH, NOW, tip=D("2"))

    assert pago.amount_received == D("30.00")
    assert pago.change == D("0.00")


def test_el_efectivo_tiene_que_cubrir_tambien_la_propina() -> None:
    pedido = _served(_item("28.00", item_id=1))

    with pytest.raises(InvalidOrder) as error:
        pedido.charge(PaymentMethod.CASH, NOW, amount_received=D("29.99"), tip=D("2"))
    assert _mensaje(error) == "El monto recibido (S/ 29.99) no cubre lo que se cobra (S/ 30.00)."
    pedido.charge(PaymentMethod.CASH, NOW, amount_received=D("30"), tip=D("2"))
    assert pedido.change == D("0.00")


def test_la_propina_no_es_negativa_ni_tiene_mas_de_dos_decimales() -> None:
    pedido = _served()

    with pytest.raises(InvalidOrder) as negativa:
        pedido.charge(PaymentMethod.CARD, NOW, tip=D("-0.01"))
    with pytest.raises(InvalidOrder) as decimales:
        pedido.charge(PaymentMethod.CARD, NOW, tip=D("0.001"))
    with pytest.raises(InvalidOrder) as recibido:
        pedido.charge(PaymentMethod.CASH, NOW, amount_received=D("50.001"))
    assert _mensaje(negativa) == "La propina no puede ser negativa."
    assert _mensaje(decimales) == "La propina admite como máximo dos decimales."
    assert _mensaje(recibido) == "El monto recibido admite como máximo dos decimales."
    assert pedido.payments == []


def test_con_tarjeta_no_se_anota_monto_recibido() -> None:
    pedido = _served()

    with pytest.raises(InvalidOrder) as error:
        pedido.charge(PaymentMethod.CARD, NOW, amount_received=D("28"))
    assert _mensaje(error) == "El monto recibido solo se anota en pagos en efectivo."
    pago = pedido.charge(PaymentMethod.CARD, NOW, tip=D("1"))
    assert (pago.amount_received, pago.change) == (None, None)


def test_el_vuelto_de_un_pago_en_efectivo_sin_monto_no_existe() -> None:
    pago = Payment(PaymentMethod.CASH, D("10.00"), received_by=1, amount_received=None)

    assert pago.change is None
    assert Payment(PaymentMethod.YAPE, D("10.00"), 1, amount_received=D("20")).change is None


def test_un_pago_por_monto_valida_monto_y_saldo() -> None:
    pedido = _served(_item("30.00", item_id=1))
    saldo = pedido.balance

    with pytest.raises(InvalidOrder) as sin_saldo:
        pedido.add_payment(PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"))
    with pytest.raises(InvalidOrder) as cero:
        pedido.add_payment(
            PaymentMethod.YAPE, NOW, received_by=7, amount=D("0"), expected_balance=saldo
        )
    with pytest.raises(InvalidOrder) as de_mas:
        pedido.add_payment(
            PaymentMethod.YAPE, NOW, received_by=7, amount=D("30.01"), expected_balance=saldo
        )
    with pytest.raises(InvalidOrder) as decimales:
        pedido.add_payment(
            PaymentMethod.YAPE, NOW, received_by=7, amount=D("1.005"), expected_balance=saldo
        )
    with pytest.raises(InvalidOrder) as ambos:
        pedido.add_payment(
            PaymentMethod.YAPE,
            NOW,
            received_by=7,
            amount=D("10"),
            item_ids=[1],
            expected_balance=saldo,
        )

    assert _mensaje(sin_saldo) == "Un pago por monto necesita el saldo que se vio al cobrar."
    assert _mensaje(cero) == "El monto de un pago tiene que ser mayor que cero."
    assert _mensaje(de_mas) == "El monto (S/ 30.01) pasa lo que falta pagar (S/ 30.00)."
    assert _mensaje(decimales) == "El monto admite como máximo dos decimales."
    assert _mensaje(ambos) == "Un pago va por monto o por platos, no por las dos cosas."
    assert pedido.payments == []


def test_un_pago_por_el_saldo_exacto_cierra_la_cuenta() -> None:
    pedido = _served(_item("30.00", item_id=1))

    pago = pedido.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("30"), expected_balance=D("30.00")
    )

    assert pago.amount == D("30.00")
    assert pedido.status is OrderStatus.PAID


def test_un_pago_parcial_deja_el_pedido_servido() -> None:
    pedido = _served(_item("30.00", item_id=1))

    pedido.add_payment(
        PaymentMethod.YAPE, LATER, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )

    assert pedido.status is OrderStatus.SERVED
    assert pedido.paid_at is None
    assert pedido.balance == D("20.00")
    assert pedido.updated_at == LATER


def test_el_saldo_cambiado_informa_el_saldo_real() -> None:
    pedido = _served(_item("30.00", item_id=1))

    with pytest.raises(BalanceChanged) as error:
        pedido.add_payment(PaymentMethod.CASH, NOW, received_by=7, expected_balance=D("29.00"))
    assert error.value.balance == D("30.00")
    assert (
        _mensaje(error) == "La cuenta cambió mientras se cobraba: ahora faltan S/ 30.00. Revísala."
    )


def test_pagar_por_platos_cobra_lo_suyo_y_recuerda_quien_pago_que() -> None:
    pedido = _served(_item("20.00", item_id=1), _item("15.00", item_id=2), _item("5.00", item_id=3))

    pago = pedido.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[2, 2, 3])

    assert pago.amount == D("20.00")
    assert pago.item_ids == (2, 3)
    assert pedido.paid_item_ids == frozenset({2, 3})
    assert pedido.balance == D("20.00")
    with pytest.raises(InvalidOrder) as repetido:
        pedido.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[3])
    with pytest.raises(OrderItemNotFound):
        pedido.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[99])
    assert _mensaje(repetido) == "Lomo saltado ya se pagó."


def test_los_platos_que_pasan_el_saldo_no_se_cobran() -> None:
    pedido = _served(_item("20.00", item_id=1), _item("20.00", item_id=2))
    pedido.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("25"), expected_balance=D("40.00")
    )

    with pytest.raises(InvalidOrder) as error:
        pedido.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[1])
    assert _mensaje(error) == "Esos platos (S/ 20.00) pasan lo que falta pagar (S/ 15.00)."


def test_los_platos_que_igualan_el_saldo_si_se_cobran() -> None:
    pedido = _served(_item("20.00", item_id=1), _item("20.00", item_id=2), _item("5", item_id=3))
    pedido.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("25"), expected_balance=D("45.00")
    )

    pago = pedido.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[1])

    assert pago.amount == D("20.00")
    assert pedido.balance == D("0.00")
    assert pedido.status is OrderStatus.PAID


# -- Descuentos y cortesías ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("porcentaje", "mensaje"),
    [
        ("-0.01", "El descuento va de 0 a 100 %."),
        ("100.01", "El descuento va de 0 a 100 %."),
        ("10.005", "El descuento admite como máximo dos decimales."),
    ],
)
def test_un_descuento_fuera_de_rango_se_rechaza(porcentaje: str, mensaje: str) -> None:
    pedido = _served()

    with pytest.raises(InvalidOrder) as error:
        pedido.apply_discount(D(porcentaje), "Frecuente", 5, NOW, limit=None)
    assert _mensaje(error) == mensaje
    assert pedido.discount_percent == 0


def test_el_descuento_del_cien_por_ciento_deja_la_cuenta_en_cero() -> None:
    pedido = _served(_item("28.00", item_id=1))

    pedido.apply_discount(D("100"), "Cumpleaños", 5, LATER, limit=None)

    assert pedido.total == D("0.00")
    assert (pedido.discount_percent, pedido.discount_reason, pedido.discounted_by) == (
        D("100.00"),
        "Cumpleaños",
        5,
    )
    assert pedido.updated_at == LATER


def test_el_descuento_justo_en_el_tope_se_aplica() -> None:
    pedido = _served()

    pedido.apply_discount(D("10"), "Amigo", 7, NOW, limit=D("10"))

    assert pedido.discount_percent == D("10.00")


def test_el_tope_del_descuento_viene_en_el_mensaje() -> None:
    pedido = _served()

    with pytest.raises(DiscountNotAllowed) as error:
        pedido.apply_discount(D("10.01"), "Amigo", 7, NOW, limit=D("10"))
    assert error.value.limit == D("10")
    assert _mensaje(error) == (
        "Tu descuento máximo es 10 %. Uno mayor, o una cortesía, lo aplica el encargado."
    )


def test_un_descuento_de_cero_lo_quita_sin_pedir_motivo() -> None:
    pedido = _served()
    pedido.apply_discount(D("15"), "Frecuente", 5, NOW, limit=None)

    pedido.apply_discount(D("0"), "", 5, LATER, limit=D("5"))

    assert (pedido.discount_percent, pedido.discount_reason, pedido.discounted_by) == (
        D("0.00"),
        "",
        None,
    )


def test_un_descuento_exige_motivo_con_tope_de_largo() -> None:
    pedido = _served()

    with pytest.raises(InvalidOrder) as sin_motivo:
        pedido.apply_discount(D("5"), "   ", 5, NOW, limit=None)
    with pytest.raises(InvalidOrder) as largo:
        pedido.apply_discount(D("5"), "m" * 201, 5, NOW, limit=None)
    assert _mensaje(sin_motivo) == "El descuento exige indicar el motivo."
    assert _mensaje(largo) == "El motivo admite 200 caracteres como máximo."
    assert validate_adjustment_reason("m" * 200, "X") == "m" * 200


def test_cortesia_y_su_reversion() -> None:
    pedido = _order(_item(item_id=1))

    invitado = pedido.grant_courtesy(1, "  Error  de cocina ", LATER)
    assert (invitado.is_courtesy, invitado.courtesy_reason) == (True, "Error de cocina")
    assert pedido.updated_at == LATER
    assert pedido.total == D("0.00")

    later = LATER + timedelta(minutes=1)
    revertido = pedido.revoke_courtesy(1, later)
    assert (revertido.is_courtesy, revertido.courtesy_reason) == (False, "")
    assert pedido.updated_at == later
    assert pedido.total == D("28.00")


def test_una_cortesia_sin_motivo_no_se_anota() -> None:
    pedido = _order(_item(item_id=1))

    with pytest.raises(InvalidOrder) as error:
        pedido.grant_courtesy(1, "  ", LATER)
    assert _mensaje(error) == "La cortesía exige indicar el motivo."
    with pytest.raises(OrderItemNotFound):
        pedido.grant_courtesy(2, "Invita", LATER)
    with pytest.raises(OrderItemNotFound):
        pedido.revoke_courtesy(2, LATER)


def test_con_pagos_o_cerrado_no_hay_cortesias_ni_descuentos() -> None:
    pedido = _served(_item("30.00", item_id=1))
    pedido.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )

    with pytest.raises(OrderHasPayments) as invitar:
        pedido.grant_courtesy(1, "Invita", LATER)
    with pytest.raises(OrderHasPayments) as quitar:
        pedido.revoke_courtesy(1, LATER)
    assert _mensaje(invitar) == "Un pedido con pagos registrados no se puede invitarle un plato."
    assert _mensaje(quitar) == "Un pedido con pagos registrados no se puede quitarle una cortesía."
    assert invitar.value.action == "invitarle un plato"

    cerrado = _order(_item(item_id=1))
    cerrado.cancel("Se fueron", NOW)
    with pytest.raises(InvalidTransition) as descontar:
        cerrado.apply_discount(D("5"), "x", 1, NOW, limit=None)
    assert _mensaje(descontar) == "Un pedido «Cancelado» no se puede aplicarle un descuento."


# -- Mesas: mover y unir ---------------------------------------------------------------------


def test_mover_de_mesa_cambia_la_mesa_y_la_fecha() -> None:
    pedido = _order(_item(item_id=1))

    pedido.move_to_table(8, LATER)

    assert pedido.table_id == 8
    assert pedido.updated_at == LATER
    assert pedido.status_changed_at != LATER


def test_mover_a_la_misma_mesa_o_un_pedido_sin_mesa_se_rechaza() -> None:
    pedido = _order(_item(item_id=1))
    para_llevar = _order(_item(item_id=1), type=OrderType.TAKEAWAY, table_id=None)

    with pytest.raises(InvalidOrder) as misma:
        pedido.move_to_table(3, LATER)
    with pytest.raises(InvalidOrder) as sin_mesa:
        para_llevar.move_to_table(3, LATER)
    assert _mensaje(misma) == "El pedido ya está en esa mesa."
    assert _mensaje(sin_mesa) == "Un pedido para llevar o delivery no ocupa mesa."
    assert para_llevar.table_id is None
    assert pedido.updated_at != LATER


def test_un_pedido_cerrado_no_cambia_de_mesa() -> None:
    pedido = _served()
    pedido.charge(PaymentMethod.CARD, NOW)

    with pytest.raises(InvalidTransition) as error:
        pedido.move_to_table(8, LATER)
    assert _mensaje(error) == "Un pedido «Pagado» no se puede cambiar de mesa."
    assert pedido.table_id == 3


def test_unir_mesas_pasa_los_platos_junta_notas_y_toma_el_estado_menos_avanzado() -> None:
    queda = _served(_item(item_id=1), id=10, notes="sin ají")
    se_une = _order(_item("5.00", item_id=2), id=11, number=15, table_id=4, notes="con hielo")
    se_une.send_to_kitchen(NOW)

    queda.absorb(se_une, LATER)

    assert [i.id for i in queda.items] == [1, 2]
    assert se_une.items == []
    assert queda.status is OrderStatus.IN_KITCHEN
    assert queda.status_changed_at == LATER
    assert queda.notes == "sin ají · con hielo"
    assert queda.updated_at == LATER
    assert (se_une.status, se_une.merged_into_id, se_une.cancelled_at) == (
        OrderStatus.CANCELLED,
        10,
        LATER,
    )
    assert se_une.cancel_reason == "Unido al pedido #14"


def test_unir_con_una_mesa_mas_avanzada_conserva_el_estado_y_la_nota() -> None:
    queda = _order(_item(item_id=1), id=10, notes="sin ají")
    se_une = _served(_item(item_id=2), id=11, table_id=4, notes="sin ají")

    queda.absorb(se_une, LATER)

    assert queda.status is OrderStatus.OPEN
    assert queda.status_changed_at != LATER
    assert queda.notes == "sin ají"


def test_unir_junta_notas_sin_pasar_el_largo_maximo() -> None:
    queda = _order(_item(item_id=1), id=10, notes="a" * 290)
    se_une = _order(_item(item_id=2), id=11, table_id=4, notes="b" * 20)

    queda.absorb(se_une, LATER)

    assert len(queda.notes) == MAX_ORDER_NOTES_LENGTH
    assert queda.notes.startswith("a" * 290 + " · ")


def test_unir_con_una_mesa_sin_nota_deja_la_propia() -> None:
    queda = _order(_item(item_id=1), id=10)
    se_une = _order(_item(item_id=2), id=11, table_id=4, notes="con hielo")

    queda.absorb(se_une, LATER)

    assert queda.notes == "con hielo"


def test_no_se_une_consigo_mismo_ni_cerrado_ni_con_pagos_ni_para_llevar() -> None:
    base = _order(_item(item_id=1), id=10)
    cerrado = _order(_item(item_id=2), id=11, table_id=4)
    cerrado.cancel("Se fueron", NOW)
    con_pago = _served(_item("30", item_id=3), id=12, table_id=5)
    con_pago.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )
    llevar = _order(_item(item_id=4), id=13, type=OrderType.TAKEAWAY, table_id=None)

    with pytest.raises(InvalidOrder) as mismo:
        base.absorb(base, LATER)
    with pytest.raises(InvalidTransition) as de_cerrado:
        base.absorb(cerrado, LATER)
    with pytest.raises(OrderHasPayments) as de_pagos:
        base.absorb(con_pago, LATER)
    with pytest.raises(InvalidOrder) as de_llevar:
        base.absorb(llevar, LATER)
    with pytest.raises(InvalidOrder):
        llevar.absorb(base, LATER)

    assert _mensaje(mismo) == "No se puede unir un pedido consigo mismo."
    assert _mensaje(de_cerrado) == "Un pedido «Cancelado» no se puede unir."
    assert _mensaje(de_pagos) == "Un pedido con pagos registrados no se puede unir."
    assert _mensaje(de_llevar) == "Solo se unen pedidos en mesa."
    assert [i.id for i in base.items] == [1]


# -- Cancelar ---------------------------------------------------------------------------------


def test_el_motivo_de_cancelacion_tiene_tope() -> None:
    pedido = _order(_item(item_id=1))

    with pytest.raises(InvalidOrder) as largo:
        pedido.cancel("m" * 301, NOW)
    with pytest.raises(InvalidOrder) as vacio:
        pedido.cancel(" \n ", NOW)
    assert _mensaje(largo) == "El motivo admite 300 caracteres como máximo."
    assert _mensaje(vacio) == "Cancelar un pedido exige indicar el motivo."
    pedido.cancel("m" * 300, LATER)
    assert pedido.status_changed_at == LATER


def test_un_pedido_con_pagos_o_ya_cerrado_no_se_cancela() -> None:
    pedido = _served(_item("30", item_id=1))
    pedido.add_payment(
        PaymentMethod.YAPE, NOW, received_by=7, amount=D("10"), expected_balance=D("30.00")
    )
    cancelado = _order(_item(item_id=1))
    cancelado.cancel("Se fueron", NOW)

    with pytest.raises(OrderHasPayments) as con_pagos:
        pedido.cancel("Error", NOW)
    with pytest.raises(InvalidTransition) as otra_vez:
        cancelado.cancel("Otra vez", NOW)
    assert _mensaje(con_pagos) == "Un pedido con pagos registrados no se puede cancelar."
    assert _mensaje(otra_vez) == "Un pedido «Cancelado» no se puede cancelar."
    assert cancelado.cancel_reason == "Se fueron"


# -- Mensajes de los errores ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fabrica", "mensaje"),
    [
        (lambda: TableNotFound(3), "No existe la mesa 3."),
        (lambda: TableLabelTaken("Barra"), "Ya existe una mesa llamada 'Barra'."),
        (lambda: TableInactive("Barra"), "La mesa Barra está desactivada."),
        (lambda: TableOccupied("Barra", 9), "La mesa Barra ya tiene un pedido activo."),
        (
            lambda: InvalidTableOrdering(),
            "El nuevo orden tiene que nombrar exactamente una vez a cada mesa del local.",
        ),
        (lambda: OrderNotFound(4), "No existe el pedido 4."),
        (lambda: DishNotFound(5), "No existe el plato 5."),
        (lambda: DishUnavailable("Ceviche"), "Ceviche no está disponible hoy."),
        (
            lambda: OrderNumberTaken(),
            "Otro pedido tomó el mismo número a la vez. Vuelve a intentarlo.",
        ),
        (lambda: CustomerNotFound(6), "No existe el cliente 6."),
        (
            lambda: NotYourOrder("cobrarlo"),
            "Solo el mesero que tomó el pedido o el encargado pueden cobrarlo.",
        ),
        (
            lambda: CashRegisterClosed(),
            "La caja está cerrada. El encargado tiene que abrirla para cobrar.",
        ),
        (
            lambda: CashRegisterAlreadyOpen(),
            "Ya hay una caja abierta. Ciérrala antes de abrir otra.",
        ),
        (lambda: CashSessionNotFound(), "No hay una caja abierta."),
        (lambda: CashSessionNotFound(8), "No existe el turno de caja 8."),
        (lambda: InvalidTable("Mesa rara"), "Mesa rara"),
        (lambda: InvalidCashSession("Caja rara"), "Caja rara"),
    ],
)
def test_cada_error_explica_lo_que_paso(fabrica: Callable[[], Exception], mensaje: str) -> None:
    assert str(fabrica()) == mensaje


def test_los_errores_guardan_sus_datos() -> None:
    assert TableNotFound(3).table_id == 3
    assert TableLabelTaken("Barra").label == "Barra"
    assert TableInactive("Barra").label == "Barra"
    ocupada = TableOccupied("Barra", 9)
    assert (ocupada.label, ocupada.order_id) == ("Barra", 9)
    assert OrderNotFound(4).order_id == 4
    assert DishNotFound(5).menu_item_id == 5
    assert DishUnavailable("Ceviche").name == "Ceviche"
    assert CustomerNotFound(6).customer_id == 6
    assert NotYourOrder("cobrarlo").action == "cobrarlo"
    assert CashSessionNotFound().session_id is None
    assert CashSessionNotFound(8).session_id == 8
    assert InvalidTable("x").reason == "x"
    assert InvalidCashSession("y").reason == "y"
    assert InvalidOrder("z").reason == "z"


# -- Caja -----------------------------------------------------------------------------------------


def test_la_caja_nueva_esta_abierta_y_sin_diferencia() -> None:
    caja = CashSession(1, 2, D("100"), opening_notes="  sencillo  ")

    assert caja.is_open
    assert caja.difference is None
    assert str(caja.opening_amount) == "100.00"
    assert caja.opening_notes == "sencillo"


def test_cerrar_la_caja_fija_el_arqueo_y_la_diferencia() -> None:
    caja = CashSession(1, 2, D("100"))

    caja.close(D("149.5"), D("150.004"), actor_id=3, now=LATER, notes="  faltó sencillo ")

    assert not caja.is_open
    assert (caja.counted_cash, caja.expected_cash) == (D("149.50"), D("150.00"))
    assert caja.difference == D("-0.50")
    assert (caja.closed_by, caja.closed_at, caja.closing_notes) == (3, LATER, "faltó sencillo")
    with pytest.raises(InvalidTransition) as error:
        caja.close(D("1"), D("1"), actor_id=3, now=LATER, notes="")
    assert _mensaje(error) == "Un pedido «Cerrada» no se puede cerrar otra vez."


def test_la_diferencia_necesita_contado_y_esperado() -> None:
    caja = CashSession(1, 2, D("100"), counted_cash=D("10"))

    assert caja.difference is None
    caja.expected_cash = D("12")
    assert caja.difference == D("-2.00")


@pytest.mark.parametrize(
    ("monto", "mensaje"),
    [
        ("-0.01", "El monto inicial no puede ser negativo."),
        ("10.005", "El monto inicial admite como máximo dos decimales."),
        ("100000000.00", "El monto inicial es demasiado grande."),
    ],
)
def test_un_monto_de_caja_invalido_se_rechaza(monto: str, mensaje: str) -> None:
    with pytest.raises(InvalidCashSession) as error:
        CashSession(1, 2, D(monto))
    assert _mensaje(error) == mensaje


def test_los_montos_limite_de_la_caja_se_aceptan() -> None:
    assert CashSession(1, 2, D("0")).opening_amount == D("0.00")
    assert validate_cash_amount(MAX_CASH_AMOUNT, "El monto") == MAX_CASH_AMOUNT


def test_la_nota_de_caja_tiene_tope() -> None:
    assert len(CashSession(1, 2, D("0"), opening_notes="n" * 300).opening_notes) == 300
    with pytest.raises(InvalidCashSession) as error:
        CashSession(1, 2, D("0"), opening_notes="n" * 301)
    assert _mensaje(error) == "La nota de la caja admite 300 caracteres como máximo."
    caja = CashSession(1, 2, D("0"))
    with pytest.raises(InvalidCashSession):
        caja.close(D("1"), D("1"), actor_id=1, now=NOW, notes="n" * 301)
    assert caja.is_open


def _pago(pedido: int, mesero: int, metodo: PaymentMethod, monto: str, propina: str = "0"):
    return CashPayment(pedido, pedido, mesero, 99, metodo, D(monto), D(propina), NOW)


def test_el_arqueo_suma_por_medio_y_por_mesero() -> None:
    caja = CashSession(1, 2, D("100"))
    pagos = [
        _pago(1, 7, PaymentMethod.CASH, "30.00", "2.00"),
        _pago(1, 7, PaymentMethod.YAPE, "10.00", "1.00"),
        _pago(2, 8, PaymentMethod.CASH, "20.00", "5.00"),
        _pago(3, 9, PaymentMethod.CARD, "50.00"),
    ]
    ajustes = [
        CashOrderAdjustment(1, D("3.00"), D("10.00")),
        CashOrderAdjustment(2, D("0"), D("0")),
    ]

    resumen = summarize_cash(caja, iter(pagos), iter(ajustes), {7: "Ana", 8: "Beto"})

    assert [(m.method, m.payments, m.amount, m.tips) for m in resumen.by_method] == [
        (PaymentMethod.CASH, 2, D("50.00"), D("7.00")),
        (PaymentMethod.YAPE, 1, D("10.00"), D("1.00")),
        (PaymentMethod.CARD, 1, D("50.00"), D("0.00")),
    ]
    # Primero quien más propina recibió; a igual propina, quien más vendió.
    assert [(w.waiter_id, w.name, w.payments, w.sales, w.tips) for w in resumen.by_waiter] == [
        (8, "Beto", 1, D("20.00"), D("5.00")),
        (7, "Ana", 2, D("40.00"), D("3.00")),
        (9, "", 1, D("50.00"), D("0.00")),
    ]
    assert resumen.opening_amount == D("100.00")
    assert (resumen.sales, resumen.tips) == (D("110.00"), D("8.00"))
    # Inicial 100 + efectivo 50 + propinas en efectivo 7.
    assert resumen.expected_cash == D("157.00")
    assert resumen.paid_orders == 3
    assert (resumen.discounts, resumen.discounted_orders, resumen.courtesies) == (
        D("3.00"),
        1,
        D("10.00"),
    )


def test_el_arqueo_desempata_por_venta_y_luego_por_nombre() -> None:
    caja = CashSession(1, 2, D("0"))
    pagos = [
        _pago(1, 1, PaymentMethod.YAPE, "10.00", "1.00"),
        _pago(2, 2, PaymentMethod.YAPE, "30.00", "1.00"),
        _pago(3, 3, PaymentMethod.YAPE, "10.00", "1.00"),
    ]

    resumen = summarize_cash(caja, pagos, [], {1: "Zoila", 2: "Mario", 3: "Ana"})

    assert [w.name for w in resumen.by_waiter] == ["Mario", "Ana", "Zoila"]


def test_un_turno_sin_pagos_espera_solo_el_inicial() -> None:
    resumen = summarize_cash(CashSession(1, 2, D("80")), [], [], {})

    assert resumen.by_method == ()
    assert resumen.by_waiter == ()
    assert (resumen.sales, resumen.tips, resumen.expected_cash) == (
        D("0.00"),
        D("0.00"),
        D("80.00"),
    )
    assert (resumen.paid_orders, resumen.discounted_orders) == (0, 0)


# -- Mesas ---------------------------------------------------------------------------------------


def test_la_mesa_limpia_su_nombre_y_valida_el_largo() -> None:
    mesa = DiningTable(1, "  Terraza   2 ")

    assert mesa.label == "Terraza 2"
    mesa.relabel("B" * MAX_LABEL_LENGTH)
    assert len(mesa.label) == MAX_LABEL_LENGTH
    with pytest.raises(InvalidTable) as largo:
        mesa.relabel("B" * (MAX_LABEL_LENGTH + 1))
    with pytest.raises(InvalidTable) as vacio:
        DiningTable(1, "   ")
    assert _mensaje(largo) == "El nombre de la mesa no puede pasar de 30 caracteres."
    assert _mensaje(vacio) == "El nombre de la mesa no puede quedar vacío."


def test_reordenar_mesas_exige_una_permutacion_exacta() -> None:
    mesas = [DiningTable(1, str(i), id=i) for i in (1, 2, 3)]

    ordenadas = reorder_tables(mesas, [3, 1, 2])

    assert [(m.id, m.position) for m in ordenadas] == [(3, 0), (1, 1), (2, 2)]
    for ids in ([1, 2], [1, 2, 3, 4], [1, 1, 2]):
        with pytest.raises(InvalidTableOrdering):
            reorder_tables(mesas, ids)


# -- Opciones elegidas al pedir -------------------------------------------------------------------


def _grupos() -> list[DishOptionGroup]:
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


def test_las_opciones_elegidas_se_congelan_con_su_precio() -> None:
    elegidas = choose_modifiers(
        "Pizza", _grupos(), [("tamaño", "FAMILIAR"), ("Extras", "queso"), ("extras", "Huevo")]
    )

    assert [(c.group, c.option, c.price) for c in elegidas] == [
        ("Tamaño", "Familiar", D("10")),
        ("Extras", "Queso", D("2")),
        ("Extras", "Huevo", D("1.5")),
    ]


@pytest.mark.parametrize(
    ("elegidas", "mensaje"),
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
def test_una_eleccion_invalida_se_rechaza(elegidas: list[tuple[str, str]], mensaje: str) -> None:
    with pytest.raises(InvalidOrder) as error:
        choose_modifiers("Pizza", _grupos(), elegidas)
    assert _mensaje(error) == mensaje


def test_sin_grupos_no_hay_nada_que_elegir() -> None:
    assert choose_modifiers("Chicha", [], []) == ()
