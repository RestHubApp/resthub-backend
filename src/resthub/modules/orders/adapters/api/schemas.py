"""Contrato HTTP de mesas y pedidos.

Los montos viajan como `Decimal`, que en JSON se escribe como texto ("45.50").
Ningún cuerpo acepta `restaurant_id` ni `waiter_id`: salen de la credencial.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from resthub.modules.orders.domain.cash import (
    MAX_CASH_NOTES_LENGTH,
    CashSession,
    MethodTotal,
    WaiterTips,
)
from resthub.modules.orders.domain.orders import (
    MAX_ADDRESS_LENGTH,
    MAX_ADJUSTMENT_REASON_LENGTH,
    MAX_CANCEL_REASON_LENGTH,
    MAX_CUSTOMER_NAME_LENGTH,
    MAX_ITEM_NOTES_LENGTH,
    MAX_ORDER_NOTES_LENGTH,
    MAX_PHONE_LENGTH,
    MAX_QUANTITY,
    MAX_REFERENCE_LENGTH,
    MIXED_PAYMENT_LABEL,
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    Payment,
    PaymentMethod,
)
from resthub.modules.orders.domain.tables import MAX_LABEL_LENGTH, DiningTable, TableStatus
from resthub.modules.orders.use_cases.cash import CashView
from resthub.modules.orders.use_cases.manage_tables import TableView
from resthub.modules.orders.use_cases.shared import OrderView

# -- Mesas ------------------------------------------------------------------


class TableResponse(BaseModel):
    id: int
    label: str
    position: int
    is_active: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, table: DiningTable) -> TableResponse:
        return cls(
            id=table.id or 0,
            label=table.label,
            position=table.position,
            is_active=table.is_active,
            created_at=table.created_at,
        )


class ActiveOrderSummary(BaseModel):
    """Lo justo para pintar la tarjeta de una mesa ocupada."""

    id: int
    number: int
    status: OrderStatus
    status_label: str
    total: Decimal
    # Lo que falta pagar; menor que el total si ya pagó una parte de la mesa.
    balance: Decimal
    item_count: int
    waiter_id: int
    waiter_name: str
    created_at: datetime
    updated_at: datetime
    # Desde cuándo está en su estado actual: cuánto lleva en cocina o listo.
    status_changed_at: datetime


class TableStateResponse(TableResponse):
    status: TableStatus
    status_label: str
    active_order: ActiveOrderSummary | None

    @classmethod
    def from_view(cls, view: TableView) -> TableStateResponse:
        table, order = view.table, view.active_order
        return cls(
            id=table.id or 0,
            label=table.label,
            position=table.position,
            is_active=table.is_active,
            created_at=table.created_at,
            status=view.status,
            status_label=view.status.label,
            active_order=(
                ActiveOrderSummary(
                    id=order.id or 0,
                    number=order.number,
                    status=order.status,
                    status_label=order.status.label,
                    total=order.total,
                    balance=order.balance,
                    item_count=order.item_count,
                    waiter_id=order.waiter_id,
                    waiter_name=view.waiter_name,
                    created_at=order.created_at,
                    updated_at=order.updated_at,
                    status_changed_at=order.status_changed_at or order.created_at,
                )
                if order is not None
                else None
            ),
        )


class CreateTableRequest(BaseModel):
    label: str = Field(min_length=1, max_length=MAX_LABEL_LENGTH)


class UpdateTableRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=MAX_LABEL_LENGTH)
    is_active: bool | None = None


class ReorderTablesRequest(BaseModel):
    ids: list[int] = Field(description="Todas las mesas, en el orden nuevo")


# -- Pedidos ----------------------------------------------------------------


class ChosenModifierResponse(BaseModel):
    group: str
    option: str
    price: Decimal


class OrderItemResponse(BaseModel):
    id: int
    menu_item_id: int
    name: str
    # Con las opciones elegidas ya sumadas.
    unit_price: Decimal
    quantity: int
    notes: str
    modifiers: list[ChosenModifierResponse]
    subtotal: Decimal
    # La casa lo invita: se sirve y no se cobra.
    is_courtesy: bool
    courtesy_reason: str
    # Ya lo pagó alguien, en una cuenta dividida por platos.
    is_paid: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, item: OrderItem, paid: bool = False) -> OrderItemResponse:
        return cls(
            id=item.id or 0,
            menu_item_id=item.menu_item_id,
            name=item.name,
            unit_price=item.unit_price,
            quantity=item.quantity,
            notes=item.notes,
            modifiers=[
                ChosenModifierResponse(group=m.group, option=m.option, price=m.price)
                for m in item.modifiers
            ],
            subtotal=item.subtotal,
            is_courtesy=item.is_courtesy,
            courtesy_reason=item.courtesy_reason,
            is_paid=paid,
            created_at=item.created_at,
        )


class PaymentResponse(BaseModel):
    id: int
    method: PaymentMethod
    method_label: str
    # Lo que se abonó a la cuenta, sin la propina.
    amount: Decimal
    tip: Decimal
    amount_received: Decimal | None
    change: Decimal | None
    received_by: int
    received_by_name: str
    item_ids: list[int]
    created_at: datetime

    @classmethod
    def from_entity(cls, payment: Payment, names: Mapping[int, str]) -> PaymentResponse:
        return cls(
            id=payment.id or 0,
            method=payment.method,
            method_label=payment.method.label,
            amount=payment.amount,
            tip=payment.tip,
            amount_received=payment.amount_received,
            change=payment.change,
            received_by=payment.received_by,
            received_by_name=names.get(payment.received_by, ""),
            item_ids=list(payment.item_ids),
            created_at=payment.created_at,
        )


class OrderResponse(BaseModel):
    id: int
    number: int
    business_date: date
    type: OrderType
    type_label: str
    status: OrderStatus
    status_label: str
    table_id: int | None
    table_label: str | None
    customer_name: str
    customer_phone: str
    delivery_address: str
    delivery_reference: str
    customer_id: int | None
    waiter_id: int
    waiter_name: str
    notes: str
    items: list[OrderItemResponse]
    item_count: int
    # Los platos a precio de carta, cortesías incluidas.
    subtotal: Decimal
    courtesy_amount: Decimal
    discount_percent: Decimal
    discount_amount: Decimal
    discount_reason: str
    discounted_by_name: str | None
    # Lo que se cobra: subtotal menos cortesías menos descuento. Sin propinas.
    total: Decimal
    paid_amount: Decimal
    balance: Decimal
    tips: Decimal
    payments: list[PaymentResponse]
    cancel_reason: str
    # `null` sin cobrar o si se pagó con más de un medio (la etiqueta dice
    # entonces «Mixto» y el detalle está en `payments`).
    payment_method: PaymentMethod | None
    payment_method_label: str | None
    amount_received: Decimal | None
    # El vuelto de un pago único en efectivo; `null` en otro caso.
    change: Decimal | None
    created_at: datetime
    # Cambia con cualquier edición, también de notas o del cliente.
    updated_at: datetime
    # Cambia solo cuando cambia el estado; el tablero mide con él el tiempo en
    # cocina o esperando a servirse.
    status_changed_at: datetime
    paid_at: datetime | None
    cancelled_at: datetime | None
    # Si se unió a otra mesa, el pedido que se quedó con sus platos.
    merged_into_id: int | None

    @classmethod
    def from_view(cls, view: OrderView) -> OrderResponse:
        order: Order = view.order
        paid_items = order.paid_item_ids
        if order.is_mixed_payment:
            method_label: str | None = MIXED_PAYMENT_LABEL
        else:
            method_label = order.payment_method.label if order.payment_method else None
        return cls(
            id=order.id or 0,
            number=order.number,
            business_date=order.business_date,
            type=order.type,
            type_label=order.type.label,
            status=order.status,
            status_label=order.status.label,
            table_id=order.table_id,
            table_label=view.table_label,
            customer_name=order.customer_name,
            customer_phone=order.customer_phone,
            delivery_address=order.delivery_address,
            delivery_reference=order.delivery_reference,
            customer_id=order.customer_id,
            waiter_id=order.waiter_id,
            waiter_name=view.waiter_name,
            notes=order.notes,
            items=[
                OrderItemResponse.from_entity(item, item.id in paid_items) for item in order.items
            ],
            item_count=order.item_count,
            subtotal=order.subtotal,
            courtesy_amount=order.courtesy_amount,
            discount_percent=order.discount_percent,
            discount_amount=order.discount_amount,
            discount_reason=order.discount_reason,
            discounted_by_name=(
                view.staff_names.get(order.discounted_by, "")
                if order.discounted_by is not None
                else None
            ),
            total=order.total,
            paid_amount=order.paid_amount,
            balance=order.balance,
            tips=order.tips,
            payments=[
                PaymentResponse.from_entity(payment, view.staff_names) for payment in order.payments
            ],
            cancel_reason=order.cancel_reason,
            payment_method=order.payment_method,
            payment_method_label=method_label,
            amount_received=order.amount_received,
            change=order.change,
            created_at=order.created_at,
            updated_at=order.updated_at,
            status_changed_at=order.status_changed_at or order.created_at,
            paid_at=order.paid_at,
            cancelled_at=order.cancelled_at,
            merged_into_id=order.merged_into_id,
        )


class OrderPageResponse(BaseModel):
    items: list[OrderResponse]
    total: int


class ChosenModifierRequest(BaseModel):
    group: str = Field(min_length=1, max_length=40)
    option: str = Field(min_length=1, max_length=40)


class NewItemRequest(BaseModel):
    menu_item_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1, le=MAX_QUANTITY)
    notes: str = Field(default="", max_length=MAX_ITEM_NOTES_LENGTH)
    # Tamaño, término, extras: lo elegido en los grupos de opciones del plato.
    modifiers: list[ChosenModifierRequest] = Field(default_factory=list, max_length=40)


class OpenOrderRequest(BaseModel):
    type: OrderType
    # Obligatoria en mesa y prohibida para llevar o delivery.
    table_id: int | None = Field(default=None, ge=1)
    customer_name: str = Field(default="", max_length=MAX_CUSTOMER_NAME_LENGTH)
    # Delivery: obligatorios teléfono y dirección (o un cliente que los tenga).
    customer_phone: str = Field(default="", max_length=MAX_PHONE_LENGTH)
    delivery_address: str = Field(default="", max_length=MAX_ADDRESS_LENGTH)
    delivery_reference: str = Field(default="", max_length=MAX_REFERENCE_LENGTH)
    customer_id: int | None = Field(default=None, ge=1)
    # Lo genera el celular; un reintento con el mismo valor no duplica el pedido.
    client_request_id: str | None = Field(default=None, min_length=8, max_length=64)
    notes: str = Field(default="", max_length=MAX_ORDER_NOTES_LENGTH)
    items: list[NewItemRequest] = Field(default_factory=list)


class AddItemsRequest(BaseModel):
    items: list[NewItemRequest] = Field(min_length=1)


class ChangeItemRequest(BaseModel):
    quantity: int | None = Field(default=None, ge=1, le=MAX_QUANTITY)
    notes: str | None = Field(default=None, max_length=MAX_ITEM_NOTES_LENGTH)


class UpdateOrderRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=MAX_ORDER_NOTES_LENGTH)
    customer_name: str | None = Field(default=None, max_length=MAX_CUSTOMER_NAME_LENGTH)
    customer_phone: str | None = Field(default=None, max_length=MAX_PHONE_LENGTH)
    delivery_address: str | None = Field(default=None, max_length=MAX_ADDRESS_LENGTH)
    delivery_reference: str | None = Field(default=None, max_length=MAX_REFERENCE_LENGTH)


class ChargeOrderRequest(BaseModel):
    payment_method: PaymentMethod
    # Solo en efectivo, propina incluida. Sin valor se entiende que pagó justo.
    amount_received: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    tip: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=10, decimal_places=2)
    # El saldo que se ve en pantalla. Si otro pago entró antes, responde 409.
    expected_balance: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)


class PaymentRequest(ChargeOrderRequest):
    """Un pago de una parte de la cuenta: por monto o por platos.

    Sin `amount` ni `item_ids` se paga lo que falta. Con `item_ids`, el monto lo
    calcula el servidor con el descuento aplicado. Con `amount`, es una parte
    libre, como cada cuota de una cuenta dividida en partes iguales.
    """

    amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    item_ids: list[int] = Field(default_factory=list)


class DiscountRequest(BaseModel):
    # Cero quita el descuento.
    percent: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    reason: str = Field(default="", max_length=MAX_ADJUSTMENT_REASON_LENGTH)


class MoveOrderRequest(BaseModel):
    table_id: int = Field(ge=1)


class MergeOrdersRequest(BaseModel):
    # La otra mesa: sus platos pasan a este pedido y ella queda libre.
    source_order_id: int = Field(ge=1)


class CourtesyRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_ADJUSTMENT_REASON_LENGTH)


class CancelOrderRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_CANCEL_REASON_LENGTH)


# -- Caja -------------------------------------------------------------------


class MethodTotalResponse(BaseModel):
    method: PaymentMethod
    method_label: str
    payments: int
    amount: Decimal
    tips: Decimal

    @classmethod
    def from_entity(cls, total: MethodTotal) -> MethodTotalResponse:
        return cls(
            method=total.method,
            method_label=total.method.label,
            payments=total.payments,
            amount=total.amount,
            tips=total.tips,
        )


class WaiterTipsResponse(BaseModel):
    waiter_id: int
    name: str
    payments: int
    sales: Decimal
    # Lo que hay que entregarle al mesero al cerrar.
    tips: Decimal

    @classmethod
    def from_entity(cls, row: WaiterTips) -> WaiterTipsResponse:
        return cls(
            waiter_id=row.waiter_id,
            name=row.name,
            payments=row.payments,
            sales=row.sales,
            tips=row.tips,
        )


class CashSummaryResponse(BaseModel):
    opening_amount: Decimal
    by_method: list[MethodTotalResponse]
    by_waiter: list[WaiterTipsResponse]
    sales: Decimal
    tips: Decimal
    # El inicial más lo cobrado en efectivo, propinas en efectivo incluidas.
    expected_cash: Decimal
    paid_orders: int
    discounts: Decimal
    discounted_orders: int
    courtesies: Decimal


class CashSessionResponse(BaseModel):
    id: int
    is_open: bool
    opened_by: int
    opened_by_name: str
    opened_at: datetime
    opening_amount: Decimal
    opening_notes: str
    closed_by: int | None
    closed_by_name: str | None
    closed_at: datetime | None
    counted_cash: Decimal | None
    expected_cash: Decimal | None
    # Contado menos esperado: positivo sobra, negativo falta.
    difference: Decimal | None
    closing_notes: str
    summary: CashSummaryResponse | None
    open_orders: int

    @classmethod
    def from_session(
        cls, session: CashSession, opened_by_name: str = "", closed_by_name: str = ""
    ) -> CashSessionResponse:
        return cls(
            id=session.id or 0,
            is_open=session.is_open,
            opened_by=session.opened_by,
            opened_by_name=opened_by_name,
            opened_at=session.opened_at,
            opening_amount=session.opening_amount,
            opening_notes=session.opening_notes,
            closed_by=session.closed_by,
            closed_by_name=closed_by_name if session.closed_by is not None else None,
            closed_at=session.closed_at,
            counted_cash=session.counted_cash,
            expected_cash=session.expected_cash,
            difference=session.difference,
            closing_notes=session.closing_notes,
            summary=None,
            open_orders=0,
        )

    @classmethod
    def from_view(cls, view: CashView) -> CashSessionResponse:
        response = cls.from_session(view.session, view.opened_by_name, view.closed_by_name)
        summary = view.summary
        response.summary = CashSummaryResponse(
            opening_amount=summary.opening_amount,
            by_method=[MethodTotalResponse.from_entity(total) for total in summary.by_method],
            by_waiter=[WaiterTipsResponse.from_entity(row) for row in summary.by_waiter],
            sales=summary.sales,
            tips=summary.tips,
            expected_cash=summary.expected_cash,
            paid_orders=summary.paid_orders,
            discounts=summary.discounts,
            discounted_orders=summary.discounted_orders,
            courtesies=summary.courtesies,
        )
        response.open_orders = view.open_orders
        return response


class CurrentCashResponse(BaseModel):
    """Si se puede cobrar. El detalle del turno solo lo ve quien maneja la caja."""

    is_open: bool
    session: CashSessionResponse | None


class CashSessionPageResponse(BaseModel):
    items: list[CashSessionResponse]
    total: int


class OpenCashRequest(BaseModel):
    opening_amount: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    notes: str = Field(default="", max_length=MAX_CASH_NOTES_LENGTH)


class CloseCashRequest(BaseModel):
    counted_cash: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    notes: str = Field(default="", max_length=MAX_CASH_NOTES_LENGTH)
