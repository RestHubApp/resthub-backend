"""Proveedores y órdenes de compra.

Python puro. Una orden de compra se arma como borrador (a mano o desde las
sugerencias de reposición), se envía al proveedor y, cuando llega la
mercadería, se recibe: lo recibido entra al libro de stock como compras, con
el costo real, y el costo del insumo se pondera como en cualquier compra.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from resthub.modules.inventory.domain.exceptions import InvalidPurchaseOrder, InvalidSupplier

MAX_SUPPLIER_NAME_LENGTH = 80
MAX_CONTACT_LENGTH = 80
MAX_PHONE_LENGTH = 20
MAX_PURCHASE_NOTES_LENGTH = 300
MAX_LINES = 60
_MILLI = Decimal("0.001")
_MICRO = Decimal("0.000001")
_CENT = Decimal("0.01")


def _text(raw: str, limit: int, what: str) -> str:
    text = " ".join(raw.split())
    if len(text) > limit:
        raise InvalidSupplier(f"{what} admite {limit} caracteres como máximo.")
    return text


@dataclass(slots=True)
class Supplier:
    restaurant_id: int
    name: str
    contact: str = ""
    phone: str = ""
    notes: str = ""
    is_active: bool = True
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.name = _text(self.name, MAX_SUPPLIER_NAME_LENGTH, "El nombre del proveedor")
        if not self.name:
            raise InvalidSupplier("El proveedor necesita un nombre.")
        self.contact = _text(self.contact, MAX_CONTACT_LENGTH, "El contacto")
        self.phone = _text(self.phone, MAX_PHONE_LENGTH, "El teléfono")
        self.notes = _text(self.notes, MAX_PURCHASE_NOTES_LENGTH, "La nota")


class PurchaseOrderStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    RECEIVED = "received"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


_STATUS_LABELS: dict[PurchaseOrderStatus, str] = {
    PurchaseOrderStatus.DRAFT: "Borrador",
    PurchaseOrderStatus.SENT: "Enviada",
    PurchaseOrderStatus.RECEIVED: "Recibida",
    PurchaseOrderStatus.CANCELLED: "Cancelada",
}


@dataclass(slots=True)
class PurchaseOrderLine:
    ingredient_id: int
    # En la unidad del insumo (g, ml o unidades), como el libro de stock.
    quantity: Decimal
    # Costo estimado por unidad del insumo; al recibir se anota el real.
    unit_cost: Decimal
    received_quantity: Decimal | None = None
    received_unit_cost: Decimal | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        self.quantity = _positive(self.quantity, "La cantidad")
        self.unit_cost = _cost(self.unit_cost)

    @property
    def estimated_total(self) -> Decimal:
        return (self.quantity * self.unit_cost).quantize(_CENT)

    @property
    def received_total(self) -> Decimal:
        if self.received_quantity is None or self.received_unit_cost is None:
            return Decimal("0.00")
        return (self.received_quantity * self.received_unit_cost).quantize(_CENT)


@dataclass(frozen=True, slots=True)
class Receipt:
    """Lo que llegó de una línea: cantidad y costo real por unidad."""

    line_id: int
    quantity: Decimal
    unit_cost: Decimal


@dataclass(slots=True)
class PurchaseOrder:
    restaurant_id: int
    number: int
    supplier_id: int
    created_by: int
    lines: list[PurchaseOrderLine] = field(default_factory=list)
    notes: str = ""
    status: PurchaseOrderStatus = PurchaseOrderStatus.DRAFT
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sent_at: datetime | None = None
    received_at: datetime | None = None
    cancelled_at: datetime | None = None

    def __post_init__(self) -> None:
        self.notes = _text(self.notes, MAX_PURCHASE_NOTES_LENGTH, "La nota")
        self.lines = validate_lines(self.lines)

    @property
    def estimated_total(self) -> Decimal:
        return sum((line.estimated_total for line in self.lines), Decimal("0.00"))

    @property
    def received_total(self) -> Decimal:
        return sum((line.received_total for line in self.lines), Decimal("0.00"))

    def replace_lines(self, lines: Iterable[PurchaseOrderLine], notes: str | None) -> None:
        self._ensure(PurchaseOrderStatus.DRAFT, "editar")
        self.lines = validate_lines(lines)
        if notes is not None:
            self.notes = _text(notes, MAX_PURCHASE_NOTES_LENGTH, "La nota")

    def send(self, now: datetime) -> None:
        self._ensure(PurchaseOrderStatus.DRAFT, "enviar")
        self.status = PurchaseOrderStatus.SENT
        self.sent_at = now

    def receive(self, receipts: Iterable[Receipt], now: datetime) -> list[PurchaseOrderLine]:
        """Anota lo que llegó; devuelve las líneas que entran al stock.

        Una línea que no aparece en lo recibido no llegó (cero). Se puede
        recibir menos de lo pedido, o más si el proveedor mandó de más.
        """
        if self.status not in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SENT):
            raise InvalidPurchaseOrder(f"Una orden «{self.status.label}» ya no se recibe.")
        by_id = {line.id: line for line in self.lines}
        for receipt in receipts:
            line = by_id.get(receipt.line_id)
            if line is None:
                raise InvalidPurchaseOrder(f"La orden no tiene la línea {receipt.line_id}.")
            line.received_quantity = _non_negative(receipt.quantity)
            line.received_unit_cost = _cost(receipt.unit_cost)
        for line in self.lines:
            if line.received_quantity is None:
                line.received_quantity = Decimal("0.000")
                line.received_unit_cost = line.unit_cost
        self.status = PurchaseOrderStatus.RECEIVED
        self.received_at = now
        return [line for line in self.lines if (line.received_quantity or 0) > 0]

    def cancel(self, now: datetime) -> None:
        if self.status not in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SENT):
            raise InvalidPurchaseOrder(f"Una orden «{self.status.label}» ya no se cancela.")
        self.status = PurchaseOrderStatus.CANCELLED
        self.cancelled_at = now

    def _ensure(self, status: PurchaseOrderStatus, action: str) -> None:
        if self.status is not status:
            raise InvalidPurchaseOrder(f"Una orden «{self.status.label}» no se puede {action}.")


def validate_lines(lines: Iterable[PurchaseOrderLine]) -> list[PurchaseOrderLine]:
    listed = list(lines)
    if not listed:
        raise InvalidPurchaseOrder("La orden necesita al menos un insumo.")
    if len(listed) > MAX_LINES:
        raise InvalidPurchaseOrder(f"Una orden admite hasta {MAX_LINES} insumos.")
    ids = [line.ingredient_id for line in listed]
    if len(set(ids)) != len(ids):
        raise InvalidPurchaseOrder("Un insumo aparece dos veces en la orden.")
    return listed


def _positive(value: Decimal, what: str) -> Decimal:
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise InvalidPurchaseOrder(f"{what} tiene que ser mayor que cero.")
    return number.quantize(_MILLI)


def _non_negative(value: Decimal) -> Decimal:
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise InvalidPurchaseOrder("Lo recibido no puede ser negativo.")
    return number.quantize(_MILLI)


def _cost(value: Decimal) -> Decimal:
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise InvalidPurchaseOrder("El costo no puede ser negativo.")
    return number.quantize(_MICRO)


@dataclass(frozen=True, slots=True)
class PurchaseSuggestion:
    """Cuánto conviene pedir de un insumo que se está acabando."""

    ingredient_id: int
    stock: Decimal
    min_stock: Decimal
    average_daily_use: Decimal
    quantity: Decimal
    unit_cost: Decimal


# Se pide para cubrir una semana de consumo o el doble del mínimo, lo que sea mayor.
COVER_DAYS = 7
# Un insumo con menos de tres días de cobertura ya entra en la lista.
ALERT_DAYS = 3


def suggest_purchase(
    ingredient_id: int,
    stock: Decimal,
    min_stock: Decimal,
    average_daily_use: Decimal,
    unit_cost: Decimal,
) -> PurchaseSuggestion | None:
    """La cantidad a pedir, o `None` si el insumo está bien cubierto.

    Cuentas en código, deterministas: la IA de reposición dice cuándo comprar;
    esto dice cuánto, para que la orden salga armada.
    """
    bajo_minimo = stock < min_stock
    poca_cobertura = average_daily_use > 0 and stock < average_daily_use * ALERT_DAYS
    if not (bajo_minimo or poca_cobertura):
        return None
    target = max(min_stock * 2, average_daily_use * COVER_DAYS)
    quantity = (target - max(stock, Decimal(0))).quantize(_MILLI)
    if quantity <= 0:
        return None
    return PurchaseSuggestion(
        ingredient_id=ingredient_id,
        stock=stock,
        min_stock=min_stock,
        average_daily_use=average_daily_use.quantize(_MILLI),
        quantity=quantity,
        unit_cost=unit_cost,
    )
