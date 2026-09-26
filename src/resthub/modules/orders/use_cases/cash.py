"""La caja del local: abrirla, ver cómo va, cerrarla con su arqueo.

Abrir, cerrar y ver los arqueos exige `cash.manage` (el encargado). Saber si
la caja está abierta lo necesita también el mesero, que cobra en ella.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Principal, Role
from resthub.core.pagination import Page
from resthub.core.realtime import EventPublisher, RealtimeEvent
from resthub.modules.orders.domain.cash import CashSession, CashSummary, summarize_cash
from resthub.modules.orders.domain.exceptions import (
    CashRegisterAlreadyOpen,
    CashSessionNotFound,
)
from resthub.modules.orders.ports.cash_register import CashRegister
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.ports.staff_directory import StaffDirectory

# Las pantallas de cobro escuchan este tema para saber si ya se puede cobrar.
CASH_TOPIC = "cash"


def announce_cash(events: EventPublisher, session: CashSession) -> None:
    events.publish(
        RealtimeEvent(
            restaurant_id=session.restaurant_id,
            topic=CASH_TOPIC,
            user_ids=frozenset(),
            roles=frozenset(Role),
            reference_id=session.id,
        )
    )


@dataclass(frozen=True, slots=True)
class CashView:
    session: CashSession
    summary: CashSummary
    # Pedidos todavía sin cobrar al momento de mirar: un cierre con pedidos
    # abiertos es válido (siguen en el turno que viene), pero conviene verlo.
    open_orders: int
    opened_by_name: str
    closed_by_name: str


class DescribeCash:
    def __init__(self, cash: CashRegister, orders: OrderRepository, staff: StaffDirectory) -> None:
        self._cash = cash
        self._orders = orders
        self._staff = staff

    async def __call__(self, session: CashSession) -> CashView:
        restaurant_id = session.restaurant_id
        payments = await self._cash.payments(restaurant_id, session.id or 0)
        adjustments = await self._cash.adjustments(restaurant_id, session.id or 0)
        people = {payment.waiter_id for payment in payments} | {session.opened_by}
        if session.closed_by is not None:
            people.add(session.closed_by)
        names = await self._staff.names(restaurant_id, people)
        open_orders = len(await self._orders.list_active(restaurant_id)) if session.is_open else 0
        return CashView(
            session=session,
            summary=summarize_cash(session, payments, adjustments, names),
            open_orders=open_orders,
            opened_by_name=names.get(session.opened_by, ""),
            closed_by_name=names.get(session.closed_by, "") if session.closed_by else "",
        )


@dataclass(frozen=True, slots=True)
class OpenCashCommand:
    actor: Principal
    opening_amount: Decimal
    notes: str = ""


class OpenCash:
    def __init__(
        self, cash: CashRegister, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._cash = cash
        self._activity = activity
        self._events = events

    async def __call__(self, command: OpenCashCommand) -> CashSession:
        actor = command.actor
        if await self._cash.current(actor.restaurant_id, for_update=True) is not None:
            raise CashRegisterAlreadyOpen()
        opened = await self._cash.open(
            CashSession(
                restaurant_id=actor.restaurant_id,
                opened_by=actor.user_id,
                opening_amount=command.opening_amount,
                opening_notes=command.notes,
                opened_at=datetime.now(UTC),
            )
        )
        await self._activity.record(
            actor.restaurant_id,
            actor.user_id,
            ActivityKind.CASH_OPENED,
            f"Turno {opened.id} con S/ {opened.opening_amount} de inicial",
        )
        announce_cash(self._events, opened)
        return opened


@dataclass(frozen=True, slots=True)
class CloseCashCommand:
    actor: Principal
    counted_cash: Decimal
    notes: str = ""


class CloseCash:
    """Cierra el turno con el efectivo contado; la diferencia queda firmada."""

    def __init__(
        self,
        cash: CashRegister,
        describe: DescribeCash,
        activity: ActivityRecorder,
        events: EventPublisher,
    ) -> None:
        self._cash = cash
        self._describe = describe
        self._activity = activity
        self._events = events

    async def __call__(self, command: CloseCashCommand) -> CashView:
        actor = command.actor
        # Tomada: un cobro que llega ahora espera y, cuando pasa, ya no hay
        # caja abierta, en vez de caer en un turno ya arqueado.
        session = await self._cash.current(actor.restaurant_id, for_update=True)
        if session is None:
            raise CashSessionNotFound()
        view = await self._describe(session)
        session.close(
            command.counted_cash,
            view.summary.expected_cash,
            actor.user_id,
            datetime.now(UTC),
            command.notes,
        )
        closed = await self._cash.save(session)
        difference = closed.difference or Decimal("0.00")
        await self._activity.record(
            actor.restaurant_id,
            actor.user_id,
            ActivityKind.CASH_CLOSED,
            f"Turno {closed.id}: contó S/ {closed.counted_cash}, esperado S/ "
            f"{closed.expected_cash}, diferencia S/ {difference}",
        )
        announce_cash(self._events, closed)
        return await self._describe(closed)


class ReadCurrentCash:
    def __init__(self, cash: CashRegister) -> None:
        self._cash = cash

    async def __call__(self, restaurant_id: int) -> CashSession | None:
        return await self._cash.current(restaurant_id)


class ReadCashSession:
    def __init__(self, cash: CashRegister) -> None:
        self._cash = cash

    async def __call__(self, restaurant_id: int, session_id: int) -> CashSession:
        session = await self._cash.get(restaurant_id, session_id)
        if session is None:
            raise CashSessionNotFound(session_id)
        return session


class ListCashSessions:
    def __init__(self, cash: CashRegister) -> None:
        self._cash = cash

    async def __call__(self, restaurant_id: int, limit: int, offset: int) -> Page[CashSession]:
        return await self._cash.history(restaurant_id, limit, offset)
