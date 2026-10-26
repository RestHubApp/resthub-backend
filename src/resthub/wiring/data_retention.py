"""La purga de los datos personales que ya cumplieron su plazo (Ley N.º 29733).

La ley pide guardar un dato personal solo mientras sirva a la finalidad con que se
recogió (ASVS 14.1.2). Los plazos están en `Settings` y en
`docs/clasificacion-de-datos.md`:

- la ficha de un cliente sin pedidos ni reservas en 24 meses se anonimiza con la
  misma regla que el derecho de cancelación (`AnonymizeCustomer`): la ficha queda,
  sin nada que lo identifique, para que las ventas sigan cuadrando;
- el contacto y las notas de un pedido se vacían a los 90 días de pagado o
  cancelado; el pedido, sus platos y montos se quedan para las ventas y el BI;
- el nombre, el teléfono y la nota de una reserva, a los 90 días de la fecha;
- el nombre y el correo de una cuenta del personal, al año de desactivada;
- los asientos de las bitácoras se borran a los dos años. La base no deja borrar
  uno más reciente (migración `0046`), ni siquiera a esta purga.

Los comprobantes no se tocan: la ley tributaria obliga a conservarlos, y su plazo
lo decide el contador.

Es de la raíz de composición porque cruza módulos: `customers`, `orders`,
`reservations`, `accounts`, `platform` e `insights`. `main.py` la arranca. Con
varias réplicas puede correr en más de una a la vez: cada paso es idempotente.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import (
    ColumnElement,
    Delete,
    String,
    and_,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from resthub.core.activity_log import ActivityRow
from resthub.core.clock import Clock
from resthub.core.config import Settings
from resthub.core.logs import get_logger
from resthub.modules.accounts.adapters.persistence.models import UserRow
from resthub.modules.customers.adapters.persistence.models import CustomerRow
from resthub.modules.customers.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyCustomerRepository,
)
from resthub.modules.customers.domain.customers import ANONYMIZED_NAME
from resthub.modules.insights.adapters.persistence.models import AiDecisionRow
from resthub.modules.orders.adapters.persistence.models import OrderItemRow, OrderRow
from resthub.modules.orders.domain.orders import OrderStatus
from resthub.modules.platform.adapters.persistence.models import PlatformActivityRow
from resthub.modules.reservations.adapters.persistence.models import ReservationRow
from resthub.wiring.customer_records import SqlAlchemyLinkedRecordsDirectory

logger = get_logger("resthub.retention")

# Cuántas fichas o asientos por pasada: una purga atrasada (la primera, o tras una
# caída larga) se reparte en varias transacciones cortas y no bloquea las tablas.
BATCH_SIZE = 500
# Nombre y dominio de una cuenta del personal anonimizada. El correo sigue siendo
# único (lleva el id) y `.invalid` es un dominio reservado: nunca recibe correo.
STAFF_PURGED_NAME = "Cuenta dada de baja"
STAFF_PURGED_DOMAIN = "@cuentas.invalid"

_CLOSED_ORDERS = (OrderStatus.PAID.value, OrderStatus.CANCELLED.value)
# La pausa entre pasadas; las pruebas la cambian para no esperar.
_pause = asyncio.sleep


@dataclass(frozen=True, slots=True)
class RetentionPeriods:
    customers: timedelta
    order_contacts: timedelta
    reservation_contacts: timedelta
    staff: timedelta
    audit_logs: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPeriods:
        return cls(
            customers=timedelta(days=settings.customer_retention_days),
            order_contacts=timedelta(days=settings.order_contact_retention_days),
            reservation_contacts=timedelta(days=settings.reservation_contact_retention_days),
            staff=timedelta(days=settings.staff_retention_days),
            audit_logs=timedelta(days=settings.audit_log_retention_days),
        )


@dataclass(frozen=True, slots=True)
class PurgeReport:
    customers: int = 0
    orders: int = 0
    order_items: int = 0
    reservations: int = 0
    staff: int = 0
    activity_log: int = 0
    platform_activity: int = 0
    ai_decisions: int = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.customers,
                self.orders,
                self.order_items,
                self.reservations,
                self.staff,
                self.activity_log,
                self.platform_activity,
                self.ai_decisions,
            )
        )


def _changed(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _any_filled(*columns: InstrumentedAttribute[str]) -> ColumnElement[bool]:
    return or_(*(column != "" for column in columns))


async def anonymize_inactive_customers(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Fichas sin pedidos ni reservas desde `cutoff`; a lo sumo `BATCH_SIZE`."""
    recent_order = exists().where(
        OrderRow.customer_id == CustomerRow.id,
        or_(OrderRow.created_at >= cutoff, OrderRow.status.not_in(_CLOSED_ORDERS)),
    )
    recent_reservation = exists().where(
        ReservationRow.customer_id == CustomerRow.id,
        or_(ReservationRow.reserved_for >= cutoff, ReservationRow.created_at >= cutoff),
    )
    expired = await session.execute(
        select(CustomerRow.restaurant_id, CustomerRow.id)
        .where(
            CustomerRow.anonymized_at.is_(None),
            CustomerRow.created_at < cutoff,
            ~recent_order,
            ~recent_reservation,
        )
        .order_by(CustomerRow.id)
        .limit(BATCH_SIZE)
    )
    customers = SqlAlchemyCustomerRepository(session)
    linked = SqlAlchemyLinkedRecordsDirectory(session)
    count = 0
    for restaurant_id, customer_id in expired.all():
        customer = await customers.get(restaurant_id, customer_id)
        if customer is None:
            continue
        customer.anonymize(now)
        await customers.save(customer)
        await linked.anonymize(restaurant_id, customer_id, ANONYMIZED_NAME)
        count += 1
    return count


async def clear_order_contacts(session: AsyncSession, cutoff: datetime) -> tuple[int, int]:
    """Vacía contacto y notas de los pedidos cerrados antes de `cutoff`."""
    closed_at = func.coalesce(OrderRow.paid_at, OrderRow.cancelled_at, OrderRow.status_changed_at)
    closed_before = and_(OrderRow.status.in_(_CLOSED_ORDERS), closed_at < cutoff)
    items = await session.execute(
        update(OrderItemRow)
        .where(
            OrderItemRow.notes != "",
            OrderItemRow.order_id.in_(select(OrderRow.id).where(closed_before)),
        )
        .values(notes="")
        .execution_options(synchronize_session=False)
    )
    orders = await session.execute(
        update(OrderRow)
        .where(
            closed_before,
            _any_filled(
                OrderRow.customer_name,
                OrderRow.customer_phone,
                OrderRow.delivery_address,
                OrderRow.delivery_reference,
                OrderRow.notes,
            ),
        )
        .values(
            customer_name="",
            customer_phone="",
            delivery_address="",
            delivery_reference="",
            notes="",
        )
        .execution_options(synchronize_session=False)
    )
    return _changed(orders), _changed(items)


async def clear_reservation_contacts(session: AsyncSession, cutoff: datetime) -> int:
    """Nombre, teléfono y nota de las reservas para antes de `cutoff`.

    El nombre no queda vacío: una reserva sin nombre no se puede leer
    (`Reservation` lo exige), así que lleva el mismo que un cliente anonimizado.
    """
    result = await session.execute(
        update(ReservationRow)
        .where(
            ReservationRow.reserved_for < cutoff,
            or_(
                ReservationRow.customer_name != ANONYMIZED_NAME,
                _any_filled(ReservationRow.phone, ReservationRow.notes),
            ),
        )
        .values(customer_name=ANONYMIZED_NAME, phone="", notes="")
        .execution_options(synchronize_session=False)
    )
    return _changed(result)


async def anonymize_former_staff(session: AsyncSession, cutoff: datetime) -> int:
    """Nombre y correo de las cuentas desactivadas antes de `cutoff`.

    La bitácora conserva el id de la cuenta; el motivo de la baja, que es texto
    libre, también se vacía (su copia queda en el asiento de la bitácora).
    """
    result = await session.execute(
        update(UserRow)
        .where(
            UserRow.is_active.is_(False),
            UserRow.deactivated_at < cutoff,
            UserRow.email.not_like(f"%{STAFF_PURGED_DOMAIN}"),
        )
        .values(
            full_name=STAFF_PURGED_NAME,
            email=literal("baja-") + cast(UserRow.id, String) + literal(STAFF_PURGED_DOMAIN),
            deactivation_reason="",
            pin_hash=None,
        )
        .execution_options(synchronize_session=False)
    )
    return _changed(result)


def _old_entries(
    row: type[ActivityRow | PlatformActivityRow | AiDecisionRow],
    moment: InstrumentedAttribute[datetime],
    cutoff: datetime,
) -> Delete:
    oldest = select(row.id).where(moment < cutoff).order_by(row.id).limit(BATCH_SIZE)
    return delete(row).where(row.id.in_(oldest)).execution_options(synchronize_session=False)


async def _delete_in_batches(factory: async_sessionmaker[AsyncSession], statement: Delete) -> int:
    total = 0
    while True:
        async with factory() as session, session.begin():
            removed = _changed(await session.execute(statement))
        total += removed
        if removed < BATCH_SIZE:
            return total


async def purge_once(
    factory: async_sessionmaker[AsyncSession], now: datetime, periods: RetentionPeriods
) -> PurgeReport:
    """Una pasada completa; cada paso en su propia transacción."""
    customers = 0
    while True:
        async with factory() as session, session.begin():
            batch = await anonymize_inactive_customers(session, now - periods.customers, now)
        customers += batch
        if batch < BATCH_SIZE:
            break
    async with factory() as session, session.begin():
        orders, items = await clear_order_contacts(session, now - periods.order_contacts)
        reservations = await clear_reservation_contacts(session, now - periods.reservation_contacts)
        staff = await anonymize_former_staff(session, now - periods.staff)
    cutoff = now - periods.audit_logs
    return PurgeReport(
        customers=customers,
        orders=orders,
        order_items=items,
        reservations=reservations,
        staff=staff,
        activity_log=await _delete_in_batches(
            factory, _old_entries(ActivityRow, ActivityRow.occurred_at, cutoff)
        ),
        platform_activity=await _delete_in_batches(
            factory, _old_entries(PlatformActivityRow, PlatformActivityRow.created_at, cutoff)
        ),
        ai_decisions=await _delete_in_batches(
            factory, _old_entries(AiDecisionRow, AiDecisionRow.created_at, cutoff)
        ),
    )


async def run_once(
    factory: async_sessionmaker[AsyncSession], clock: Clock, periods: RetentionPeriods
) -> PurgeReport:
    report = await purge_once(factory, clock.now(), periods)
    if report.total:
        logger.info(
            "retention.purged",
            customers=report.customers,
            orders=report.orders,
            order_items=report.order_items,
            reservations=report.reservations,
            staff=report.staff,
            activity_log=report.activity_log,
            platform_activity=report.platform_activity,
            ai_decisions=report.ai_decisions,
        )
    return report


async def _loop(
    factory: async_sessionmaker[AsyncSession], clock: Clock, settings: Settings
) -> None:
    periods = RetentionPeriods.from_settings(settings)
    while True:
        await _pause(settings.data_retention_interval_seconds)
        try:
            await run_once(factory, clock, periods)
        except Exception:
            logger.exception("retention.purge_failed")


class DataRetentionLoop:
    """La tarea de fondo: `start` al arrancar el API y `stop` al apagarlo."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], clock: Clock, settings: Settings
    ) -> None:
        self._factory, self._clock, self._settings = factory, clock, settings
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._settings.data_retention_interval_seconds > 0:
            self._task = asyncio.create_task(
                _loop(self._factory, self._clock, self._settings), name="data-retention"
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
