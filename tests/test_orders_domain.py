"""Reglas de dominio de los pedidos, sin base ni servidor."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from resthub.modules.orders.domain.exceptions import (
    BalanceChanged,
    DiscountNotAllowed,
    InvalidOrder,
    InvalidTransition,
    OrderHasPayments,
    OrderItemNotFound,
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


def test_el_total_suma_precio_por_cantidad() -> None:
    order = _order(_item("28.00", 2), _item("5.50", 3))

    assert order.total == Decimal("72.50")
    assert order.item_count == 5


def test_un_pedido_vacio_suma_cero() -> None:
    assert _order().total == Decimal("0.00")


def test_el_ciclo_completo() -> None:
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
    ("preparar", "accion"),
    [
        ([], "mark_ready"),
        ([], "mark_served"),
        (["send_to_kitchen"], "mark_served"),
        (["send_to_kitchen"], "send_to_kitchen"),
        (["send_to_kitchen", "mark_ready"], "mark_ready"),
    ],
)
def test_transiciones_que_saltan_pasos_se_rechazan(preparar: list[str], accion: str) -> None:
    order = _order(_item())
    for paso in preparar:
        getattr(order, paso)(NOW)

    with pytest.raises(InvalidTransition):
        getattr(order, accion)(NOW)


def test_solo_se_cobra_lo_servido() -> None:
    order = _order(_item())
    order.send_to_kitchen(NOW)
    order.mark_ready(NOW)

    with pytest.raises(InvalidTransition):
        order.charge(PaymentMethod.CASH, NOW)


def test_un_pedido_sin_platos_no_va_a_cocina() -> None:
    with pytest.raises(InvalidOrder):
        _order().send_to_kitchen(NOW)


@pytest.mark.parametrize("hasta", ["mark_ready", "mark_served"])
def test_agregar_platos_a_un_pedido_listo_o_servido_lo_devuelve_a_cocina(hasta: str) -> None:
    order = _order(_item())
    order.send_to_kitchen(NOW)
    order.mark_ready(NOW)
    if hasta == "mark_served":
        order.mark_served(NOW)

    order.add_items([_item("5.00")], NOW)

    assert order.status is OrderStatus.IN_KITCHEN
    assert order.total == Decimal("33.00")


def test_agregar_a_un_pedido_abierto_o_en_cocina_no_cambia_su_estado() -> None:
    abierto = _order()
    en_cocina = _order(_item())
    en_cocina.send_to_kitchen(NOW)

    abierto.add_items([_item()], NOW)
    en_cocina.add_items([_item()], NOW)

    assert abierto.status is OrderStatus.OPEN
    assert en_cocina.status is OrderStatus.IN_KITCHEN


def test_el_momento_del_estado_solo_lo_mueve_un_cambio_de_estado() -> None:
    abierto_a_las = datetime(2026, 9, 25, 19, 0, tzinfo=UTC)
    order = Order(
        restaurant_id=1,
        number=1,
        business_date=date(2026, 9, 25),
        type=OrderType.TAKEAWAY,
        waiter_id=7,
        items=[_item()],
        created_at=abierto_a_las,
    )
    assert order.status_changed_at == abierto_a_las

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


def test_a_un_pedido_cerrado_no_se_le_agrega_nada() -> None:
    order = _served()
    order.charge(PaymentMethod.CARD, NOW)

    with pytest.raises(InvalidTransition):
        order.add_items([_item()], NOW)


def test_quitar_y_cambiar_platos_solo_con_el_pedido_abierto() -> None:
    order = _order(_item(item_id=1), _item(item_id=2))
    order.change_item(1, NOW, quantity=3, notes="sin  cebolla")
    order.remove_item(2, NOW)

    assert [(i.id, i.quantity, i.notes) for i in order.items] == [(1, 3, "sin cebolla")]
    with pytest.raises(OrderItemNotFound):
        order.remove_item(99, NOW)

    order.send_to_kitchen(NOW)
    with pytest.raises(InvalidTransition):
        order.change_item(1, NOW, quantity=1)
    with pytest.raises(InvalidTransition):
        order.remove_item(1, NOW)


@pytest.mark.parametrize("quantity", [0, 100])
def test_la_cantidad_tiene_limites(quantity: int) -> None:
    with pytest.raises(InvalidOrder):
        _item(quantity=quantity)


def test_cancelar_exige_motivo() -> None:
    order = _order(_item())

    with pytest.raises(InvalidOrder):
        order.cancel("   ", NOW)
    order.cancel("  El cliente se  fue ", NOW)

    assert order.status is OrderStatus.CANCELLED
    assert order.cancel_reason == "El cliente se fue"
    assert order.cancelled_at == NOW


def test_lo_pagado_no_se_cancela() -> None:
    order = _served()
    order.charge(PaymentMethod.CASH, NOW)

    with pytest.raises(InvalidTransition):
        order.cancel("Error", NOW)


def test_en_efectivo_se_calcula_el_vuelto() -> None:
    order = _served(_item("28.00"), _item("5.50", 2))

    order.charge(PaymentMethod.CASH, NOW, amount_received=Decimal("50"))

    assert order.total == Decimal("39.00")
    assert order.amount_received == Decimal("50.00")
    assert order.change == Decimal("11.00")


def test_en_efectivo_sin_monto_se_entiende_pago_justo() -> None:
    order = _served()

    order.charge(PaymentMethod.CASH, NOW)

    assert order.change == Decimal("0.00")


def test_el_efectivo_tiene_que_cubrir_el_total() -> None:
    order = _served()

    with pytest.raises(InvalidOrder):
        order.charge(PaymentMethod.CASH, NOW, amount_received=Decimal("20.00"))
    assert order.status is OrderStatus.SERVED


def test_con_otro_medio_no_hay_monto_ni_vuelto() -> None:
    order = _served()

    with pytest.raises(InvalidOrder):
        order.charge(PaymentMethod.YAPE, NOW, amount_received=Decimal("50.00"))
    order.charge(PaymentMethod.PLIN, NOW)

    assert order.change is None
    assert order.payment_method is PaymentMethod.PLIN


def test_en_mesa_necesita_mesa_y_para_llevar_no_la_lleva() -> None:
    with pytest.raises(InvalidOrder):
        Order(
            restaurant_id=1,
            number=1,
            business_date=date(2026, 9, 25),
            type=OrderType.DINE_IN,
            waiter_id=1,
        )
    with pytest.raises(InvalidOrder):
        Order(
            restaurant_id=1,
            number=1,
            business_date=date(2026, 9, 25),
            type=OrderType.TAKEAWAY,
            waiter_id=1,
            table_id=2,
        )


# -- Descuentos, cortesías y pagos parciales -----------------------------------


def test_el_descuento_redondea_al_centimo_y_no_toca_las_cortesias() -> None:
    order = _served(_item("33.33", 1, item_id=1), _item("10.00", 1, item_id=2))
    order.grant_courtesy(2, "Invita la casa", NOW)

    order.apply_discount(Decimal("12.5"), "Cliente frecuente", 5, NOW, limit=None)

    # 12.5 % de 33.33 es 4.16625: se redondea a 4.17.
    assert order.courtesy_amount == Decimal("10.00")
    assert order.discount_amount == Decimal("4.17")
    assert order.total == Decimal("29.16")


def test_el_descuento_sobre_el_tope_no_se_aplica() -> None:
    order = _served()

    with pytest.raises(DiscountNotAllowed):
        order.apply_discount(Decimal("15"), "Amigo", 7, NOW, limit=Decimal("10"))

    assert order.discount_percent == 0


def test_quien_paga_los_ultimos_platos_cierra_la_cuenta_con_el_redondeo() -> None:
    order = _served(_item("10.00", 1, item_id=1), _item("10.00", 1, item_id=2))
    order.apply_discount(Decimal("33.33"), "Promoción", 5, NOW, limit=None)

    primero = order.add_payment(PaymentMethod.CASH, NOW, received_by=7, item_ids=[1])
    segundo = order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, item_ids=[2])

    # 20.00 con 33.33 % de descuento son 13.33. Cada plato solo daría 6.67 y
    # sobraría un céntimo: el último pago toma lo que falta, 6.66.
    assert order.total == Decimal("13.33")
    assert primero.amount == Decimal("6.67")
    assert segundo.amount == Decimal("6.66")
    assert order.balance == 0
    assert order.status is OrderStatus.PAID


def test_una_cuenta_toda_invitada_se_cierra_sin_monto() -> None:
    order = _served(_item("28.00", 1, item_id=1))
    order.grant_courtesy(1, "Error de cocina", NOW)

    pago = order.charge(PaymentMethod.CASH, NOW, received_by=5)

    assert pago.amount == Decimal("0.00")
    assert order.status is OrderStatus.PAID


def test_con_un_pago_hecho_no_se_descuenta_ni_se_cancela() -> None:
    order = _served(_item("28.00", 2, item_id=1))
    order.add_payment(PaymentMethod.YAPE, NOW, received_by=7, amount=Decimal("10"))

    with pytest.raises(OrderHasPayments):
        order.apply_discount(Decimal("5"), "Tarde", 7, NOW, limit=None)
    with pytest.raises(OrderHasPayments):
        order.cancel("Se fueron", NOW)
    assert order.balance == Decimal("46.00")
    assert order.status is OrderStatus.SERVED


def test_el_saldo_esperado_distinto_frena_el_cobro() -> None:
    order = _served()

    with pytest.raises(BalanceChanged):
        order.add_payment(PaymentMethod.CASH, NOW, received_by=7, expected_balance=Decimal("20.00"))
    assert order.payments == []
