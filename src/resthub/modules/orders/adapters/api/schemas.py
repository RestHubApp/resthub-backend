"""Contrato HTTP de mesas y pedidos.

Los montos viajan como `Decimal`, que en JSON se escribe como texto ("45.50").
Ningún cuerpo acepta `restaurant_id` ni `waiter_id`: salen de la credencial.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from resthub.modules.orders.domain.orders import (
    MAX_CANCEL_REASON_LENGTH,
    MAX_CUSTOMER_NAME_LENGTH,
    MAX_ITEM_NOTES_LENGTH,
    MAX_ORDER_NOTES_LENGTH,
    MAX_QUANTITY,
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    PaymentMethod,
)
from resthub.modules.orders.domain.tables import MAX_LABEL_LENGTH, DiningTable, TableStatus
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
    item_count: int
    waiter_id: int
    waiter_name: str
    created_at: datetime
    updated_at: datetime


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
                    item_count=order.item_count,
                    waiter_id=order.waiter_id,
                    waiter_name=view.waiter_name,
                    created_at=order.created_at,
                    updated_at=order.updated_at,
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


class OrderItemResponse(BaseModel):
    id: int
    menu_item_id: int
    name: str
    unit_price: Decimal
    quantity: int
    notes: str
    subtotal: Decimal
    created_at: datetime

    @classmethod
    def from_entity(cls, item: OrderItem) -> OrderItemResponse:
        return cls(
            id=item.id or 0,
            menu_item_id=item.menu_item_id,
            name=item.name,
            unit_price=item.unit_price,
            quantity=item.quantity,
            notes=item.notes,
            subtotal=item.subtotal,
            created_at=item.created_at,
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
    waiter_id: int
    waiter_name: str
    notes: str
    items: list[OrderItemResponse]
    item_count: int
    total: Decimal
    cancel_reason: str
    payment_method: PaymentMethod | None
    payment_method_label: str | None
    amount_received: Decimal | None
    # El vuelto de un pago en efectivo; `null` con otro medio o sin cobrar.
    change: Decimal | None
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None
    cancelled_at: datetime | None

    @classmethod
    def from_view(cls, view: OrderView) -> OrderResponse:
        order: Order = view.order
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
            waiter_id=order.waiter_id,
            waiter_name=view.waiter_name,
            notes=order.notes,
            items=[OrderItemResponse.from_entity(item) for item in order.items],
            item_count=order.item_count,
            total=order.total,
            cancel_reason=order.cancel_reason,
            payment_method=order.payment_method,
            payment_method_label=order.payment_method.label if order.payment_method else None,
            amount_received=order.amount_received,
            change=order.change,
            created_at=order.created_at,
            updated_at=order.updated_at,
            paid_at=order.paid_at,
            cancelled_at=order.cancelled_at,
        )


class OrderPageResponse(BaseModel):
    items: list[OrderResponse]
    total: int


class NewItemRequest(BaseModel):
    menu_item_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1, le=MAX_QUANTITY)
    notes: str = Field(default="", max_length=MAX_ITEM_NOTES_LENGTH)


class OpenOrderRequest(BaseModel):
    type: OrderType
    # Obligatoria en mesa y prohibida para llevar.
    table_id: int | None = Field(default=None, ge=1)
    customer_name: str = Field(default="", max_length=MAX_CUSTOMER_NAME_LENGTH)
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


class ChargeOrderRequest(BaseModel):
    payment_method: PaymentMethod
    # Solo en efectivo. Sin valor se entiende que el cliente pagó justo.
    amount_received: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)


class CancelOrderRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_CANCEL_REASON_LENGTH)
