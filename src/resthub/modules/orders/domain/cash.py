"""Caja: un turno de cobro del local, con su apertura, su arqueo y su cierre.

Python puro. Hay una sola caja abierta por restaurante: la abre el encargado
con el efectivo inicial, los meseros cobran en ella, y al cerrarla el encargado
cuenta el efectivo. La diferencia contra lo esperado queda registrada.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from resthub.modules.orders.domain.exceptions import InvalidCashSession, InvalidTransition
from resthub.modules.orders.domain.orders import CENT, ZERO, PaymentMethod

MAX_CASH_NOTES_LENGTH = 300
MAX_CASH_AMOUNT = Decimal("99999999.99")


@dataclass(slots=True)
class CashSession:
    restaurant_id: int
    opened_by: int
    opening_amount: Decimal
    opened_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    opening_notes: str = ""
    closed_by: int | None = None
    closed_at: datetime | None = None
    counted_cash: Decimal | None = None
    # Se fija al cerrar: lo que decía el sistema en ese momento. Así un
    # cambio posterior en los pagos no reescribe un arqueo ya firmado.
    expected_cash: Decimal | None = None
    closing_notes: str = ""
    id: int | None = None

    def __post_init__(self) -> None:
        self.opening_amount = validate_cash_amount(self.opening_amount, "El monto inicial")
        self.opening_notes = validate_cash_notes(self.opening_notes)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def difference(self) -> Decimal | None:
        """Contado menos esperado: positivo sobra, negativo falta."""
        if self.counted_cash is None or self.expected_cash is None:
            return None
        return (self.counted_cash - self.expected_cash).quantize(CENT)

    def close(
        self,
        counted_cash: Decimal,
        expected_cash: Decimal,
        actor_id: int,
        now: datetime,
        notes: str,
    ) -> None:
        if not self.is_open:
            raise InvalidTransition("Cerrada", "cerrar otra vez")
        self.counted_cash = validate_cash_amount(counted_cash, "El efectivo contado")
        self.expected_cash = expected_cash.quantize(CENT)
        self.closing_notes = validate_cash_notes(notes)
        self.closed_by = actor_id
        self.closed_at = now


@dataclass(frozen=True, slots=True)
class CashPayment:
    """Un pago cobrado en el turno, con lo que el arqueo necesita saber de él."""

    order_id: int
    order_number: int
    waiter_id: int
    received_by: int
    method: PaymentMethod
    amount: Decimal
    tip: Decimal
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CashOrderAdjustment:
    """Descuento y cortesías de un pedido cobrado en el turno."""

    order_id: int
    discount_amount: Decimal
    courtesy_amount: Decimal


@dataclass(frozen=True, slots=True)
class MethodTotal:
    method: PaymentMethod
    payments: int
    amount: Decimal
    tips: Decimal


@dataclass(frozen=True, slots=True)
class WaiterTips:
    waiter_id: int
    name: str
    payments: int
    sales: Decimal
    tips: Decimal


@dataclass(frozen=True, slots=True)
class CashSummary:
    opening_amount: Decimal
    by_method: tuple[MethodTotal, ...]
    by_waiter: tuple[WaiterTips, ...]
    sales: Decimal
    tips: Decimal
    # Lo que tiene que haber en el cajón: el inicial más lo cobrado en
    # efectivo, propinas en efectivo incluidas (se reparten al cerrar).
    expected_cash: Decimal
    paid_orders: int
    discounts: Decimal
    discounted_orders: int
    courtesies: Decimal


def summarize_cash(
    session: CashSession,
    payments: Iterable[CashPayment],
    adjustments: Iterable[CashOrderAdjustment],
    names: Mapping[int, str],
) -> CashSummary:
    """El arqueo de un turno a partir de sus pagos.

    La propina es del mesero que atendió el pedido, no de quien cobró: si un
    compañero cubrió la caja, la atención igual fue del otro.
    """
    listed = list(payments)
    amounts: dict[PaymentMethod, Decimal] = defaultdict(lambda: ZERO)
    tips_by_method: dict[PaymentMethod, Decimal] = defaultdict(lambda: ZERO)
    counts: dict[PaymentMethod, int] = defaultdict(int)
    waiter_sales: dict[int, Decimal] = defaultdict(lambda: ZERO)
    waiter_tips: dict[int, Decimal] = defaultdict(lambda: ZERO)
    waiter_counts: dict[int, int] = defaultdict(int)
    for payment in listed:
        amounts[payment.method] += payment.amount
        tips_by_method[payment.method] += payment.tip
        counts[payment.method] += 1
        waiter_sales[payment.waiter_id] += payment.amount
        waiter_tips[payment.waiter_id] += payment.tip
        waiter_counts[payment.waiter_id] += 1

    by_method = tuple(
        MethodTotal(
            method=method,
            payments=counts[method],
            amount=amounts[method].quantize(CENT),
            tips=tips_by_method[method].quantize(CENT),
        )
        for method in PaymentMethod
        if counts[method]
    )
    by_waiter = tuple(
        sorted(
            (
                WaiterTips(
                    waiter_id=waiter_id,
                    name=names.get(waiter_id, ""),
                    payments=waiter_counts[waiter_id],
                    sales=waiter_sales[waiter_id].quantize(CENT),
                    tips=waiter_tips[waiter_id].quantize(CENT),
                )
                for waiter_id in waiter_counts
            ),
            key=lambda row: (-row.tips, -row.sales, row.name),
        )
    )
    cash = PaymentMethod.CASH
    adjusted = list(adjustments)
    return CashSummary(
        opening_amount=session.opening_amount,
        by_method=by_method,
        by_waiter=by_waiter,
        sales=sum((payment.amount for payment in listed), ZERO).quantize(CENT),
        tips=sum((payment.tip for payment in listed), ZERO).quantize(CENT),
        expected_cash=(session.opening_amount + amounts[cash] + tips_by_method[cash]).quantize(
            CENT
        ),
        paid_orders=len({payment.order_id for payment in listed}),
        discounts=sum((order.discount_amount for order in adjusted), ZERO).quantize(CENT),
        discounted_orders=sum(1 for order in adjusted if order.discount_amount > 0),
        courtesies=sum((order.courtesy_amount for order in adjusted), ZERO).quantize(CENT),
    )


def validate_cash_amount(value: Decimal, what: str) -> Decimal:
    amount = Decimal(value)
    if amount < 0:
        raise InvalidCashSession(f"{what} no puede ser negativo.")
    if amount != amount.quantize(CENT):
        raise InvalidCashSession(f"{what} admite como máximo dos decimales.")
    if amount > MAX_CASH_AMOUNT:
        raise InvalidCashSession(f"{what} es demasiado grande.")
    return amount.quantize(CENT)


def validate_cash_notes(raw: str) -> str:
    notes = raw.strip()
    if len(notes) > MAX_CASH_NOTES_LENGTH:
        raise InvalidCashSession(
            f"La nota de la caja admite {MAX_CASH_NOTES_LENGTH} caracteres como máximo."
        )
    return notes
