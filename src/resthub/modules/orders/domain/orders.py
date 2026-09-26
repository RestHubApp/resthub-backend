"""Pedidos: el ciclo de vida y las cuentas.

Python puro. Toda regla sobre qué se puede hacer con un pedido en cada estado
vive acá y no en los casos de uso, para que no dependa de qué pantalla la
dispare.

El ciclo es `open → in_kitchen → ready → served → paid`, y `cancelled` desde
cualquier estado activo. Hay un solo retroceso: agregar platos a un pedido
`ready` o `served` lo devuelve a `in_kitchen`, porque la cocina tiene algo
nuevo que preparar.

Un pedido servido se cobra con uno o varios pagos (cuenta dividida o pago
mixto); queda pagado cuando lo abonado cubre el total. Descuentos y cortesías
van antes del primer pago.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from resthub.modules.orders.domain.exceptions import (
    BalanceChanged,
    DiscountNotAllowed,
    InvalidOrder,
    InvalidTransition,
    OrderHasPayments,
    OrderItemNotFound,
)
from resthub.modules.orders.domain.modifiers import ChosenModifier

MAX_QUANTITY = 99
MAX_ITEM_NOTES_LENGTH = 200
MAX_ORDER_NOTES_LENGTH = 300
MAX_CUSTOMER_NAME_LENGTH = 80
MAX_CANCEL_REASON_LENGTH = 300
MAX_DISH_NAME_LENGTH = 120
MAX_ADJUSTMENT_REASON_LENGTH = 200
MAX_PHONE_LENGTH = 20
MAX_ADDRESS_LENGTH = 200
MAX_REFERENCE_LENGTH = 150
MAX_CLIENT_REQUEST_ID_LENGTH = 64
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal(100)
# Se guarda en la columna `payment_method` cuando la cuenta se pagó con más de
# un medio. No es un `PaymentMethod`: nadie paga "en mixto".
MIXED_PAYMENT = "mixed"
MIXED_PAYMENT_LABEL = "Mixto"


class OrderType(StrEnum):
    DINE_IN = "dine_in"
    TAKEAWAY = "takeaway"
    # Reparto propio del local: se lleva a la dirección del cliente.
    DELIVERY = "delivery"

    @property
    def label(self) -> str:
        return _TYPE_LABELS[self]


_TYPE_LABELS: dict[OrderType, str] = {
    OrderType.DINE_IN: "En mesa",
    OrderType.TAKEAWAY: "Para llevar",
    OrderType.DELIVERY: "Delivery",
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
# El avance de un pedido activo. Al unir dos mesas, el pedido que queda toma
# el menos avanzado de los dos: si una seguía en cocina, la cuenta unida
# también, porque todavía hay platos por salir.
_PROGRESS: dict[OrderStatus, int] = {
    OrderStatus.OPEN: 0,
    OrderStatus.IN_KITCHEN: 1,
    OrderStatus.READY: 2,
    OrderStatus.SERVED: 3,
}

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
    # Tamaño, término, extras: congelados al pedir, con su precio. `unit_price`
    # ya los incluye.
    modifiers: tuple[ChosenModifier, ...] = ()
    # Una cortesía la invita la casa: se prepara y se sirve, pero no se cobra.
    is_courtesy: bool = False
    courtesy_reason: str = ""
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

    @property
    def due(self) -> Decimal:
        """Lo que se cobra por esta línea antes del descuento del pedido."""
        return ZERO if self.is_courtesy else self.subtotal


@dataclass(slots=True)
class Payment:
    """Un pago de la cuenta, o de una parte de ella.

    `amount` es lo que se abona a la cuenta. La propina va aparte: no es venta
    del local sino del mesero, y el cierre de caja la reparte. En efectivo,
    `amount_received` es lo que entregó el cliente, propina incluida.
    """

    method: PaymentMethod
    amount: Decimal
    received_by: int
    tip: Decimal = ZERO
    amount_received: Decimal | None = None
    cash_session_id: int | None = None
    # Los platos que esta persona pagó, cuando la cuenta se divide por platos.
    item_ids: tuple[int, ...] = ()
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def change(self) -> Decimal | None:
        if self.method is not PaymentMethod.CASH or self.amount_received is None:
            return None
        return (self.amount_received - self.amount - self.tip).quantize(CENT)


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
    # Para delivery: a dónde y a quién llamar. En mesa o para llevar, opcionales.
    customer_phone: str = ""
    delivery_address: str = ""
    delivery_reference: str = ""
    # El cliente frecuente, si se eligió de la libreta de clientes.
    customer_id: int | None = None
    # Lo genera el celular del mesero al tomar el pedido. Si el pedido se
    # reintenta (sin señal, doble toque), el mismo identificador no crea otro.
    client_request_id: str | None = None
    notes: str = ""
    status: OrderStatus = OrderStatus.OPEN
    items: list[OrderItem] = field(default_factory=list)
    cancel_reason: str = ""
    # Descuento sobre lo que se cobra (sin las cortesías), en porcentaje.
    discount_percent: Decimal = ZERO
    discount_reason: str = ""
    discounted_by: int | None = None
    payments: list[Payment] = field(default_factory=list)
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Cuándo entró al estado en que está. A diferencia de `updated_at`, editar
    # la nota o el cliente no lo mueve: el tablero mide con él cuánto lleva un
    # pedido en cocina o esperando a que lo sirvan. Sin valor, el de apertura.
    status_changed_at: datetime | None = None
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None
    # Si se unió a otra mesa: el pedido que se quedó con sus platos. Queda
    # cancelado, pero no es una venta perdida, así que los reportes lo saltan.
    merged_into_id: int | None = None

    def __post_init__(self) -> None:
        if self.status_changed_at is None:
            self.status_changed_at = self.created_at
        if self.type is OrderType.DINE_IN and self.table_id is None:
            raise InvalidOrder("Un pedido en mesa necesita la mesa.")
        if self.type is not OrderType.DINE_IN and self.table_id is not None:
            raise InvalidOrder("Un pedido para llevar o delivery no ocupa mesa.")
        self.customer_name = validate_customer_name(self.customer_name)
        self.customer_phone = _short_text(self.customer_phone, MAX_PHONE_LENGTH, "El teléfono")
        self.delivery_address = _short_text(
            self.delivery_address, MAX_ADDRESS_LENGTH, "La dirección"
        )
        self.delivery_reference = _short_text(
            self.delivery_reference, MAX_REFERENCE_LENGTH, "La referencia"
        )
        if self.type is OrderType.DELIVERY:
            _ensure_deliverable(self)
        if self.client_request_id is not None:
            self.client_request_id = self.client_request_id.strip()[:MAX_CLIENT_REQUEST_ID_LENGTH]
        self.notes = validate_order_notes(self.notes)

    # -- Cuentas ------------------------------------------------------------

    @property
    def subtotal(self) -> Decimal:
        """Lo que suman los platos a precio de carta, cortesías incluidas."""
        return sum((item.subtotal for item in self.items), ZERO).quantize(CENT)

    @property
    def courtesy_amount(self) -> Decimal:
        return sum((item.subtotal for item in self.items if item.is_courtesy), ZERO).quantize(CENT)

    @property
    def discount_amount(self) -> Decimal:
        return _percent_of(self.subtotal - self.courtesy_amount, self.discount_percent)

    @property
    def total(self) -> Decimal:
        """Lo que se cobra: platos menos cortesías menos descuento. Sin propinas."""
        return (self.subtotal - self.courtesy_amount - self.discount_amount).quantize(CENT)

    @property
    def paid_amount(self) -> Decimal:
        return sum((payment.amount for payment in self.payments), ZERO).quantize(CENT)

    @property
    def balance(self) -> Decimal:
        """Lo que falta pagar."""
        return (self.total - self.paid_amount).quantize(CENT)

    @property
    def tips(self) -> Decimal:
        return sum((payment.tip for payment in self.payments), ZERO).quantize(CENT)

    @property
    def payment_method(self) -> PaymentMethod | None:
        """El medio con que se pagó; `None` sin pagos o si se usó más de uno."""
        methods = {payment.method for payment in self.payments}
        return methods.pop() if len(methods) == 1 else None

    @property
    def is_mixed_payment(self) -> bool:
        return len({payment.method for payment in self.payments}) > 1

    @property
    def amount_received(self) -> Decimal | None:
        """Lo que entregó el cliente, solo si pagó todo de una vez en efectivo."""
        if len(self.payments) != 1:
            return None
        return self.payments[0].amount_received

    @property
    def change(self) -> Decimal | None:
        """El vuelto de un pago único en efectivo; `None` si no aplica."""
        if len(self.payments) != 1:
            return None
        return self.payments[0].change

    @property
    def paid_item_ids(self) -> frozenset[int]:
        return frozenset(item_id for payment in self.payments for item_id in payment.item_ids)

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
        self,
        now: datetime,
        notes: str | None = None,
        customer_name: str | None = None,
        delivery: tuple[str | None, str | None, str | None] | None = None,
    ) -> None:
        """Nota, cliente y, en delivery, teléfono, dirección y referencia."""
        if not self.is_active:
            raise InvalidTransition(self.status.label, "editar")
        if notes is not None:
            self.notes = validate_order_notes(notes)
        if customer_name is not None:
            self.customer_name = validate_customer_name(customer_name)
        if delivery is not None:
            phone, address, reference = delivery
            if phone is not None:
                self.customer_phone = _short_text(phone, MAX_PHONE_LENGTH, "El teléfono")
            if address is not None:
                self.delivery_address = _short_text(address, MAX_ADDRESS_LENGTH, "La dirección")
            if reference is not None:
                self.delivery_reference = _short_text(
                    reference, MAX_REFERENCE_LENGTH, "La referencia"
                )
        if self.type is OrderType.DELIVERY:
            _ensure_deliverable(self)
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
        self,
        method: PaymentMethod,
        now: datetime,
        amount_received: Decimal | None = None,
        *,
        received_by: int = 0,
        tip: Decimal = ZERO,
        cash_session_id: int | None = None,
    ) -> Payment:
        """Cobra de una vez todo lo que falta pagar del pedido ya servido."""
        return self.add_payment(
            method,
            now,
            received_by=received_by,
            amount_received=amount_received,
            tip=tip,
            cash_session_id=cash_session_id,
        )

    def add_payment(
        self,
        method: PaymentMethod,
        now: datetime,
        *,
        received_by: int,
        amount: Decimal | None = None,
        item_ids: Collection[int] = (),
        amount_received: Decimal | None = None,
        tip: Decimal = ZERO,
        cash_session_id: int | None = None,
        expected_balance: Decimal | None = None,
    ) -> Payment:
        """Registra un pago: la cuenta entera, una parte o los platos de alguien.

        Sin monto ni platos se paga lo que falta. Con platos, el monto sale de
        ellos (con el descuento del pedido aplicado). Con monto, es una parte
        libre, como en una cuenta dividida en partes iguales. Cuando lo pagado
        cubre el total, el pedido queda pagado.

        `expected_balance` es el saldo que vio quien cobra: si otro pago entró
        en el medio (o un doble toque), no coincide y no se cobra dos veces.
        """
        self._ensure_status({OrderStatus.SERVED}, "cobrar")
        if expected_balance is not None and expected_balance != self.balance:
            raise BalanceChanged(self.balance)
        tip = _money(tip, "La propina")
        if tip < 0:
            raise InvalidOrder("La propina no puede ser negativa.")
        paid_items = tuple(dict.fromkeys(item_ids))
        charged = self._amount_to_charge(amount, paid_items)

        if method is PaymentMethod.CASH:
            received = charged + tip if amount_received is None else amount_received
            received = _money(received, "El monto recibido")
            if received < charged + tip:
                raise InvalidOrder(
                    f"El monto recibido (S/ {received}) no cubre lo que se cobra "
                    f"(S/ {(charged + tip).quantize(CENT)})."
                )
        else:
            if amount_received is not None:
                raise InvalidOrder("El monto recibido solo se anota en pagos en efectivo.")
            received = None

        payment = Payment(
            method=method,
            amount=charged,
            received_by=received_by,
            tip=tip,
            amount_received=received,
            cash_session_id=cash_session_id,
            item_ids=paid_items,
            created_at=now,
        )
        self.payments.append(payment)
        self.updated_at = now
        if self.balance == ZERO:
            self.paid_at = now
            self._move_to(OrderStatus.PAID, now)
        return payment

    def apply_discount(
        self,
        percent: Decimal,
        reason: str,
        actor_id: int,
        now: datetime,
        limit: Decimal | None,
    ) -> None:
        """Descuento sobre lo que se cobra. Cero lo quita.

        `limit` es el tope de quien lo aplica (el del mesero lo fija el
        encargado); `None` es sin tope.
        """
        self._ensure_adjustable("aplicarle un descuento")
        percent = Decimal(percent)
        if percent < 0 or percent > HUNDRED:
            raise InvalidOrder("El descuento va de 0 a 100 %.")
        if percent != percent.quantize(CENT):
            raise InvalidOrder("El descuento admite como máximo dos decimales.")
        if limit is not None and percent > limit:
            raise DiscountNotAllowed(limit)
        if percent == 0:
            self.discount_percent, self.discount_reason, self.discounted_by = ZERO, "", None
        else:
            self.discount_percent = percent.quantize(CENT)
            self.discount_reason = validate_adjustment_reason(reason, "El descuento")
            self.discounted_by = actor_id
        self.updated_at = now

    def grant_courtesy(self, item_id: int, reason: str, now: datetime) -> OrderItem:
        self._ensure_adjustable("invitarle un plato")
        item = self._find_item(item_id)
        item.is_courtesy = True
        item.courtesy_reason = validate_adjustment_reason(reason, "La cortesía")
        self.updated_at = now
        return item

    def revoke_courtesy(self, item_id: int, now: datetime) -> OrderItem:
        self._ensure_adjustable("quitarle una cortesía")
        item = self._find_item(item_id)
        item.is_courtesy = False
        item.courtesy_reason = ""
        self.updated_at = now
        return item

    def move_to_table(self, table_id: int, now: datetime) -> None:
        """Cambia la mesa de un pedido en curso: los comensales se mudaron."""
        if not self.is_active:
            raise InvalidTransition(self.status.label, "cambiar de mesa")
        if self.type is not OrderType.DINE_IN:
            raise InvalidOrder("Un pedido para llevar o delivery no ocupa mesa.")
        if table_id == self.table_id:
            raise InvalidOrder("El pedido ya está en esa mesa.")
        self.table_id = table_id
        self.updated_at = now

    def absorb(self, other: Order, now: datetime) -> None:
        """Une otra mesa a esta: sus platos pasan acá y ella queda cerrada.

        Solo antes de cobrar: con pagos hechos, lo que pagó cada uno dejaría de
        cuadrar. El pedido que queda toma el estado menos avanzado de los dos.
        Los platos los mueve el repositorio; acá se ajustan las cuentas.
        """
        if other.id == self.id:
            raise InvalidOrder("No se puede unir un pedido consigo mismo.")
        for order in (self, other):
            if not order.is_active:
                raise InvalidTransition(order.status.label, "unir")
            if order.payments:
                raise OrderHasPayments("unir")
            if order.type is not OrderType.DINE_IN:
                raise InvalidOrder("Solo se unen pedidos en mesa.")
        self.items.extend(other.items)
        other.items = []
        if _PROGRESS[other.status] < _PROGRESS[self.status]:
            self._move_to(other.status, now)
        if other.notes and other.notes not in self.notes:
            joined = " · ".join(filter(None, (self.notes, other.notes)))
            self.notes = joined[:MAX_ORDER_NOTES_LENGTH]
        self.updated_at = now
        other.merged_into_id = self.id
        other.cancel_reason = f"Unido al pedido #{self.number}"
        other.cancelled_at = now
        other._move_to(OrderStatus.CANCELLED, now)

    def cancel(self, reason: str, now: datetime) -> None:
        if not self.is_active:
            raise InvalidTransition(self.status.label, "cancelar")
        if self.payments:
            # Lo cobrado ya está en la caja: cancelar lo dejaría sin pedido.
            raise OrderHasPayments("cancelar")
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

    def _ensure_adjustable(self, action: str) -> None:
        # Descuentos y cortesías cambian el total; con un pago ya hecho, lo
        # que pagó cada uno dejaría de cuadrar con lo que se le cobró.
        if not self.is_active:
            raise InvalidTransition(self.status.label, action)
        if self.payments:
            raise OrderHasPayments(action)

    def _amount_to_charge(self, amount: Decimal | None, item_ids: tuple[int, ...]) -> Decimal:
        balance = self.balance
        if item_ids and amount is not None:
            raise InvalidOrder("Un pago va por monto o por platos, no por las dos cosas.")
        if item_ids:
            already_paid = self.paid_item_ids
            lines = [self._find_item(item_id) for item_id in item_ids]
            for line in lines:
                if line.id in already_paid:
                    raise InvalidOrder(f"{line.name} ya se pagó.")
            due = sum((line.due for line in lines), ZERO)
            charged = (due - _percent_of(due, self.discount_percent)).quantize(CENT)
            pending = [
                item
                for item in self.items
                if item.id not in already_paid and item.id not in set(item_ids)
            ]
            # Quien paga los últimos platos cierra la cuenta: los centavos del
            # redondeo del descuento quedan en su parte y no sueltos.
            if not pending:
                return balance
            if charged > balance:
                raise InvalidOrder(
                    f"Esos platos (S/ {charged}) pasan lo que falta pagar (S/ {balance})."
                )
            return charged
        if amount is None:
            return balance
        charged = _money(amount, "El monto")
        if charged <= 0:
            raise InvalidOrder("El monto de un pago tiene que ser mayor que cero.")
        if charged > balance:
            raise InvalidOrder(f"El monto (S/ {charged}) pasa lo que falta pagar (S/ {balance}).")
        return charged

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


def _percent_of(amount: Decimal, percent: Decimal) -> Decimal:
    return (amount * percent / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def _money(value: Decimal, what: str) -> Decimal:
    value = Decimal(value)
    if value != value.quantize(CENT):
        raise InvalidOrder(f"{what} admite como máximo dos decimales.")
    return value.quantize(CENT)


def validate_adjustment_reason(raw: str, what: str) -> str:
    reason = " ".join(raw.split())
    if not reason:
        raise InvalidOrder(f"{what} exige indicar el motivo.")
    if len(reason) > MAX_ADJUSTMENT_REASON_LENGTH:
        raise InvalidOrder(
            f"El motivo admite {MAX_ADJUSTMENT_REASON_LENGTH} caracteres como máximo."
        )
    return reason


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


def _short_text(raw: str, limit: int, what: str) -> str:
    text = " ".join(raw.split())
    if len(text) > limit:
        raise InvalidOrder(f"{what} admite {limit} caracteres como máximo.")
    return text


def _ensure_deliverable(order: Order) -> None:
    if not order.customer_name:
        raise InvalidOrder("Un delivery necesita el nombre del cliente.")
    if not order.customer_phone:
        raise InvalidOrder("Un delivery necesita un teléfono para coordinar la entrega.")
    if not order.delivery_address:
        raise InvalidOrder("Un delivery necesita la dirección de entrega.")


def validate_customer_name(raw: str) -> str:
    name = " ".join(raw.split())
    if len(name) > MAX_CUSTOMER_NAME_LENGTH:
        raise InvalidOrder(f"El nombre del cliente admite {MAX_CUSTOMER_NAME_LENGTH} caracteres.")
    return name
