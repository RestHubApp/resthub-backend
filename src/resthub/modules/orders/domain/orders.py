"""Pedidos: el ciclo de vida y las cuentas.

Python puro. Toda regla sobre qué se puede hacer con un pedido en cada estado
vive acá y no en los casos de uso, para que no dependa de qué pantalla la
dispare.

El ciclo es `open → in_kitchen → ready → served → paid`, y `cancelled` desde
cualquier estado activo. Hay un solo retroceso: agregar platos a un pedido
`ready` o `served` lo devuelve a `in_kitchen`, porque la cocina tiene algo
nuevo que preparar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from resthub.modules.orders.domain.exceptions import (
    InvalidOrder,
    InvalidTransition,
    OrderItemNotFound,
)

MAX_QUANTITY = 99
MAX_ITEM_NOTES_LENGTH = 200
MAX_ORDER_NOTES_LENGTH = 300
MAX_CUSTOMER_NAME_LENGTH = 80
MAX_CANCEL_REASON_LENGTH = 300
MAX_DISH_NAME_LENGTH = 120
CENT = Decimal("0.01")


class OrderType(StrEnum):
    DINE_IN = "dine_in"
    TAKEAWAY = "takeaway"

    @property
    def label(self) -> str:
        return _TYPE_LABELS[self]


_TYPE_LABELS: dict[OrderType, str] = {
    OrderType.DINE_IN: "En mesa",
    OrderType.TAKEAWAY: "Para llevar",
}


class OrderStatus(StrEnum):
    OPEN = "open"
    IN_KITCHEN = "in_kitchen"
    READY = "ready"
    SERVED = "served"
    PAID = "paid"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]

    @property
    def is_active(self) -> bool:
        return self in ACTIVE_STATUSES


_STATUS_LABELS: dict[OrderStatus, str] = {
    OrderStatus.OPEN: "Abierto",
    OrderStatus.IN_KITCHEN: "En cocina",
    OrderStatus.READY: "Listo",
    OrderStatus.SERVED: "Servido",
    OrderStatus.PAID: "Pagado",
    OrderStatus.CANCELLED: "Cancelado",
}

# Un pedido activo ocupa su mesa y aparece en el tablero; pagado o cancelado,
# ya es historia.
ACTIVE_STATUSES = frozenset(
    {OrderStatus.OPEN, OrderStatus.IN_KITCHEN, OrderStatus.READY, OrderStatus.SERVED}
)


class PaymentMethod(StrEnum):
    CASH = "cash"
    YAPE = "yape"
    PLIN = "plin"
    CARD = "card"
    TRANSFER = "transfer"

    @property
    def label(self) -> str:
        return _METHOD_LABELS[self]


_METHOD_LABELS: dict[PaymentMethod, str] = {
    PaymentMethod.CASH: "Efectivo",
    PaymentMethod.YAPE: "Yape",
    PaymentMethod.PLIN: "Plin",
    PaymentMethod.CARD: "Tarjeta",
    PaymentMethod.TRANSFER: "Transferencia",
}


@dataclass(slots=True)
class OrderItem:
    """Un plato dentro del pedido.

    Nombre y precio son una foto del menú al momento de pedirlo: si mañana el
    lomo saltado sube, el pedido de hoy se sigue cobrando a lo que costaba.
    """

    menu_item_id: int
    name: str
    unit_price: Decimal
    quantity: int
    notes: str = ""
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.name = self.name.strip()[:MAX_DISH_NAME_LENGTH]
        if self.unit_price < 0:
            raise InvalidOrder("El precio de un plato no puede ser negativo.")
        self.unit_price = self.unit_price.quantize(CENT)
        self.quantity = validate_quantity(self.quantity)
        self.notes = validate_item_notes(self.notes)

    @property
    def subtotal(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(CENT)


@dataclass(slots=True)
class Order:
    restaurant_id: int
    # Correlativo del día en el local: "el 14" es lo que se grita en la cocina.
    number: int
    # El día del restaurante en que se abrió, no el de UTC; ver `local_time`.
    business_date: date
    type: OrderType
    waiter_id: int
    table_id: int | None = None
    customer_name: str = ""
    notes: str = ""
    status: OrderStatus = OrderStatus.OPEN
    items: list[OrderItem] = field(default_factory=list)
    cancel_reason: str = ""
    payment_method: PaymentMethod | None = None
    amount_received: Decimal | None = None
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Cuándo entró al estado en que está. A diferencia de `updated_at`, editar
    # la nota o el cliente no lo mueve: el tablero mide con él cuánto lleva un
    # pedido en cocina o esperando a que lo sirvan. Sin valor, el de apertura.
    status_changed_at: datetime | None = None
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.status_changed_at is None:
            self.status_changed_at = self.created_at
        if self.type is OrderType.DINE_IN and self.table_id is None:
            raise InvalidOrder("Un pedido en mesa necesita la mesa.")
        if self.type is OrderType.TAKEAWAY and self.table_id is not None:
            raise InvalidOrder("Un pedido para llevar no ocupa mesa.")
        self.customer_name = validate_customer_name(self.customer_name)
        self.notes = validate_order_notes(self.notes)

    # -- Cuentas ------------------------------------------------------------

    @property
    def total(self) -> Decimal:
        return sum((item.subtotal for item in self.items), Decimal("0.00")).quantize(CENT)

    @property
    def change(self) -> Decimal | None:
        """El vuelto de un pago en efectivo; `None` si no aplica."""
        if self.payment_method is not PaymentMethod.CASH or self.amount_received is None:
            return None
        return (self.amount_received - self.total).quantize(CENT)

    @property
    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def is_active(self) -> bool:
        return self.status.is_active

    # -- Platos -------------------------------------------------------------

    def ensure_accepts_items(self) -> None:
        if not self.is_active:
            raise InvalidTransition(self.status.label, "agregar platos")

    def add_items(self, items: list[OrderItem], now: datetime) -> None:
        self.ensure_accepts_items()
        if not items:
            raise InvalidOrder("No hay platos para agregar.")
        self.items.extend(items)
        # La cocina tiene algo nuevo que preparar. Un pedido abierto o ya en
        # cocina queda como está: todavía no salió nada que haya que rehacer.
        if self.status in (OrderStatus.READY, OrderStatus.SERVED):
            self._move_to(OrderStatus.IN_KITCHEN, now)
        self.updated_at = now

    def change_item(
        self, item_id: int, now: datetime, quantity: int | None = None, notes: str | None = None
    ) -> OrderItem:
        self._ensure_items_editable("cambiar sus platos")
        item = self._find_item(item_id)
        if quantity is not None:
            item.quantity = validate_quantity(quantity)
        if notes is not None:
            item.notes = validate_item_notes(notes)
        self.updated_at = now
        return item

    def remove_item(self, item_id: int, now: datetime) -> OrderItem:
        self._ensure_items_editable("quitarle platos")
        item = self._find_item(item_id)
        self.items.remove(item)
        self.updated_at = now
        return item

    def update_details(
        self, now: datetime, notes: str | None = None, customer_name: str | None = None
    ) -> None:
        if not self.is_active:
            raise InvalidTransition(self.status.label, "editar")
        if notes is not None:
            self.notes = validate_order_notes(notes)
        if customer_name is not None:
            self.customer_name = validate_customer_name(customer_name)
        self.updated_at = now

    # -- Estados ------------------------------------------------------------

    def send_to_kitchen(self, now: datetime) -> None:
        self._ensure_status({OrderStatus.OPEN}, "enviar a cocina")
        if not self.items:
            raise InvalidOrder("Un pedido sin platos no se envía a cocina.")
        self._move_to(OrderStatus.IN_KITCHEN, now)

    def mark_ready(self, now: datetime) -> None:
        self._ensure_status({OrderStatus.IN_KITCHEN}, "marcar como listo")
        self._move_to(OrderStatus.READY, now)

    def mark_served(self, now: datetime) -> None:
        self._ensure_status({OrderStatus.READY}, "marcar como servido")
        self._move_to(OrderStatus.SERVED, now)

    def charge(
        self, method: PaymentMethod, now: datetime, amount_received: Decimal | None = None
    ) -> None:
        """Cobra el pedido ya servido.

        En efectivo se anota cuánto entregó el cliente para calcular el vuelto;
        sin monto se entiende que pagó justo. Con otro medio el monto no
        aplica: se cobra exactamente el total.
        """
        self._ensure_status({OrderStatus.SERVED}, "cobrar")
        if method is PaymentMethod.CASH:
            received = self.total if amount_received is None else amount_received
            if received != received.quantize(CENT):
                raise InvalidOrder("El monto recibido admite como máximo dos decimales.")
            if received < self.total:
                raise InvalidOrder(
                    f"El monto recibido (S/ {received}) no cubre el total (S/ {self.total})."
                )
            self.amount_received = received.quantize(CENT)
        else:
            if amount_received is not None:
                raise InvalidOrder("El monto recibido solo se anota en pagos en efectivo.")
            self.amount_received = None
        self.payment_method = method
        self.paid_at = now
        self._move_to(OrderStatus.PAID, now)

    def cancel(self, reason: str, now: datetime) -> None:
        if not self.is_active:
            raise InvalidTransition(self.status.label, "cancelar")
        cleaned = " ".join(reason.split())
        if not cleaned:
            raise InvalidOrder("Cancelar un pedido exige indicar el motivo.")
        if len(cleaned) > MAX_CANCEL_REASON_LENGTH:
            raise InvalidOrder(
                f"El motivo admite {MAX_CANCEL_REASON_LENGTH} caracteres como máximo."
            )
        self.cancel_reason = cleaned
        self.cancelled_at = now
        self._move_to(OrderStatus.CANCELLED, now)

    # -- Internos -----------------------------------------------------------

    def _ensure_status(self, allowed: set[OrderStatus], action: str) -> None:
        if self.status not in allowed:
            raise InvalidTransition(self.status.label, action)

    def _ensure_items_editable(self, action: str) -> None:
        # Una vez en cocina, quitar o cambiar un plato es algo que hay que
        # hablar con la cocina: puede que ya esté en la sartén. Si no se va a
        # preparar, se cancela el pedido con su motivo.
        self._ensure_status({OrderStatus.OPEN}, action)

    def _find_item(self, item_id: int) -> OrderItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise OrderItemNotFound(item_id)

    def _move_to(self, status: OrderStatus, now: datetime) -> None:
        self.status = status
        self.status_changed_at = now
        self.updated_at = now


def validate_quantity(quantity: int) -> int:
    if quantity < 1 or quantity > MAX_QUANTITY:
        raise InvalidOrder(f"La cantidad tiene que estar entre 1 y {MAX_QUANTITY}.")
    return quantity


def validate_item_notes(raw: str) -> str:
    notes = " ".join(raw.split())
    if len(notes) > MAX_ITEM_NOTES_LENGTH:
        raise InvalidOrder(f"La nota del plato admite {MAX_ITEM_NOTES_LENGTH} caracteres.")
    return notes


def validate_order_notes(raw: str) -> str:
    notes = raw.strip()
    if len(notes) > MAX_ORDER_NOTES_LENGTH:
        raise InvalidOrder(f"La nota del pedido admite {MAX_ORDER_NOTES_LENGTH} caracteres.")
    return notes


def validate_customer_name(raw: str) -> str:
    name = " ".join(raw.split())
    if len(name) > MAX_CUSTOMER_NAME_LENGTH:
        raise InvalidOrder(f"El nombre del cliente admite {MAX_CUSTOMER_NAME_LENGTH} caracteres.")
    return name
