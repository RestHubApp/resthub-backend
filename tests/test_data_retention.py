"""La purga de datos personales vencidos (`wiring/data_retention.py`, Ley N.º 29733)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.activity_log import ActivityRow
from resthub.core.config import Settings
from resthub.modules.accounts.adapters.persistence.models import UserRow
from resthub.modules.customers.adapters.persistence.models import CustomerRow
from resthub.modules.customers.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyCustomerRepository,
)
from resthub.modules.customers.domain.customers import ANONYMIZED_NAME
from resthub.modules.insights.adapters.persistence.models import AiDecisionRow
from resthub.modules.menu.adapters.persistence.models import MenuCategoryRow, MenuItemRow
from resthub.modules.orders.adapters.persistence.models import OrderItemRow, OrderRow
from resthub.modules.orders.domain.orders import OrderStatus, OrderType
from resthub.modules.platform.adapters.persistence.models import (
    PlatformActivityRow,
    PlatformAdminRow,
)
from resthub.modules.reservations.adapters.persistence.models import ReservationRow
from resthub.modules.reservations.domain.reservations import ReservationStatus
from resthub.wiring import data_retention
from resthub.wiring.data_retention import (
    STAFF_PURGED_NAME,
    DataRetentionLoop,
    PurgeReport,
    RetentionPeriods,
    purge_once,
    run_once,
)
from tests.conftest import StaffedRestaurant, logged_events
from tests.fakes import FixedClock

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
PERIODS = RetentionPeriods.from_settings(Settings())


def _factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)


def _ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


async def _customer(session: AsyncSession, local: StaffedRestaurant, created_days_ago: int) -> int:
    row = CustomerRow(
        restaurant_id=local.id,
        name="Ana Quispe",
        phone="987 654 321",
        email="ana@correo.pe",
        address="Av. Arequipa 123",
        notes="Alérgica al maní",
        created_at=_ago(created_days_ago),
    )
    session.add(row)
    await session.flush()
    return row.id


async def _order(
    session: AsyncSession,
    local: StaffedRestaurant,
    *,
    status: OrderStatus = OrderStatus.PAID,
    closed_days_ago: int = 0,
    customer_id: int | None = None,
    item_note: str = "",
) -> int:
    closed = _ago(closed_days_ago)
    order = OrderRow(
        restaurant_id=local.id,
        number=1,
        business_date=closed.date(),
        type=OrderType.DELIVERY.value,
        status=status.value,
        waiter_id=local.waiter.id,
        total=Decimal("30.00"),
        customer_name="Ana Quispe",
        customer_phone="987 654 321",
        delivery_address="Av. Arequipa 123",
        delivery_reference="Frente al parque",
        notes="Tocar el timbre",
        customer_id=customer_id,
        created_at=closed,
        status_changed_at=closed,
        paid_at=closed if status is OrderStatus.PAID else None,
        cancelled_at=closed if status is OrderStatus.CANCELLED else None,
    )
    session.add(order)
    await session.flush()
    if item_note:
        category = MenuCategoryRow(restaurant_id=local.id, name="Fondos")
        session.add(category)
        await session.flush()
        dish = MenuItemRow(
            restaurant_id=local.id, category_id=category.id, name="Ají de gallina", price=10
        )
        session.add(dish)
        await session.flush()
        session.add(
            OrderItemRow(
                restaurant_id=local.id,
                order_id=order.id,
                menu_item_id=dish.id,
                name="Ají de gallina",
                unit_price=Decimal("10.00"),
                quantity=1,
                notes=item_note,
            )
        )
        await session.flush()
    return order.id


async def _reservation(
    session: AsyncSession, local: StaffedRestaurant, days_ago: int, customer_id: int | None = None
) -> int:
    row = ReservationRow(
        restaurant_id=local.id,
        customer_name="Ana Quispe",
        phone="987 654 321",
        party_size=4,
        reserved_for=_ago(days_ago),
        duration_minutes=90,
        customer_id=customer_id,
        notes="Cumpleaños",
        status=ReservationStatus.SEATED.value,
        created_by=local.admin.id,
        created_at=_ago(days_ago + 1),
    )
    session.add(row)
    await session.flush()
    return row.id


async def test_a_customer_without_visits_for_two_years_is_anonymized(
    session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    forgotten = await _customer(session, restaurant_a, created_days_ago=900)
    await _order(session, restaurant_a, closed_days_ago=800, customer_id=forgotten)
    regular = await _customer(session, restaurant_a, created_days_ago=900)
    await _order(session, restaurant_a, closed_days_ago=10, customer_id=regular)
    booked = await _customer(session, restaurant_a, created_days_ago=900)
    await _reservation(session, restaurant_a, days_ago=-5, customer_id=booked)
    newcomer = await _customer(session, restaurant_a, created_days_ago=30)
    await session.commit()

    report = await purge_once(_factory(session), NOW, PERIODS)

    assert report.customers == 1
    customers = SqlAlchemyCustomerRepository(session)
    session.expire_all()
    gone = await customers.get(restaurant_a.id, forgotten)
    assert gone is not None
    assert gone.name == ANONYMIZED_NAME
    assert (gone.phone, gone.email, gone.address, gone.notes) == ("", "", "", "")
    assert gone.anonymized_at == NOW
    for kept in (regular, booked, newcomer):
        customer = await customers.get(restaurant_a.id, kept)
        assert customer is not None
        assert customer.name == "Ana Quispe"
        assert customer.anonymized_at is None


async def test_an_open_order_keeps_its_customer(
    session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    # Un pedido que la cocina todavía tiene (aunque sea viejo) no pierde su cliente.
    customer = await _customer(session, restaurant_a, created_days_ago=900)
    order = await _order(
        session,
        restaurant_a,
        status=OrderStatus.IN_KITCHEN,
        closed_days_ago=800,
        customer_id=customer,
        item_note="Sin maní",
    )
    await session.commit()

    report = await purge_once(_factory(session), NOW, PERIODS)

    assert (report.customers, report.orders, report.order_items) == (0, 0, 0)
    row = await session.get(OrderRow, order)
    assert row is not None
    assert row.customer_phone == "987 654 321"


async def test_order_contacts_are_cleared_90_days_after_closing(
    session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    paid = await _order(session, restaurant_a, closed_days_ago=91, item_note="Sin cebolla")
    cancelled = await _order(
        session, restaurant_a, status=OrderStatus.CANCELLED, closed_days_ago=120
    )
    recent = await _order(session, restaurant_a, closed_days_ago=89, item_note="Bien cocido")
    await session.commit()

    report = await purge_once(_factory(session), NOW, PERIODS)

    assert (report.orders, report.order_items) == (2, 1)
    session.expire_all()
    for order_id in (paid, cancelled):
        row = await session.get(OrderRow, order_id)
        assert row is not None
        assert (
            row.customer_name,
            row.customer_phone,
            row.delivery_address,
            row.delivery_reference,
            row.notes,
        ) == ("", "", "", "", "")
        # Lo que cuentan las ventas se queda.
        assert row.total == Decimal("30.00")
    kept = await session.get(OrderRow, recent)
    assert kept is not None
    assert kept.customer_phone == "987 654 321"
    notes = await session.scalars(select(OrderItemRow.notes).order_by(OrderItemRow.order_id))
    assert list(notes) == ["", "Bien cocido"]


async def test_reservation_contacts_are_cleared_90_days_after_the_date(
    session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    old = await _reservation(session, restaurant_a, days_ago=91)
    recent = await _reservation(session, restaurant_a, days_ago=30)
    await session.commit()

    report = await purge_once(_factory(session), NOW, PERIODS)

    assert report.reservations == 1
    session.expire_all()
    cleared = await session.get(ReservationRow, old)
    assert cleared is not None
    assert (cleared.customer_name, cleared.phone, cleared.notes) == (ANONYMIZED_NAME, "", "")
    assert cleared.party_size == 4
    kept = await session.get(ReservationRow, recent)
    assert kept is not None
    assert kept.phone == "987 654 321"


async def _deactivate(session: AsyncSession, user_id: int, days_ago: int) -> None:
    row = await session.get(UserRow, user_id)
    assert row is not None
    row.is_active = False
    row.deactivated_at = _ago(days_ago)
    row.deactivation_reason = "Renunció por motivos de salud"


async def test_former_staff_are_anonymized_a_year_after_leaving(
    session: AsyncSession, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    await _deactivate(session, restaurant_a.waiter.id or 0, days_ago=366)
    await _deactivate(session, restaurant_b.waiter.id or 0, days_ago=364)
    await session.commit()

    report = await purge_once(_factory(session), NOW, PERIODS)

    assert report.staff == 1
    session.expire_all()
    gone = await session.get(UserRow, restaurant_a.waiter.id)
    assert gone is not None
    assert gone.full_name == STAFF_PURGED_NAME
    assert gone.email == f"baja-{gone.id}@cuentas.invalid"
    assert gone.deactivation_reason == ""
    recent = await session.get(UserRow, restaurant_b.waiter.id)
    assert recent is not None
    assert recent.full_name == "Luis Torres"
    active = await session.get(UserRow, restaurant_a.admin.id)
    assert active is not None
    assert active.full_name == "Rosa Pérez"


async def test_audit_entries_older_than_two_years_are_deleted(
    session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    admin = PlatformAdminRow(email="equipo@resthub.dev", full_name="Equipo", password_hash="x")
    session.add(admin)
    await session.flush()
    for days in (800, 10):
        session.add(
            ActivityRow(
                restaurant_id=restaurant_a.id,
                user_id=restaurant_a.admin.id,
                kind="asiento",
                occurred_at=_ago(days),
            )
        )
        session.add(PlatformActivityRow(admin_id=admin.id, kind="asiento", created_at=_ago(days)))
        session.add(
            AiDecisionRow(
                restaurant_id=restaurant_a.id,
                kind="restock",
                subject_type="supply",
                subject_id=1,
                input_state={},
                output={},
                engine="rules",
                model="",
                created_at=_ago(days),
            )
        )
    await session.commit()

    report = await purge_once(_factory(session), NOW, PERIODS)

    assert (report.activity_log, report.platform_activity, report.ai_decisions) == (1, 1, 1)
    for moment in (
        select(ActivityRow.occurred_at),
        select(PlatformActivityRow.created_at),
        select(AiDecisionRow.created_at),
    ):
        assert len(list(await session.scalars(moment))) == 1


async def test_a_large_backlog_is_purged_in_batches(
    session: AsyncSession, restaurant_a: StaffedRestaurant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(data_retention, "BATCH_SIZE", 2)
    for _ in range(5):
        await _customer(session, restaurant_a, created_days_ago=900)
        session.add(
            ActivityRow(
                restaurant_id=restaurant_a.id,
                user_id=restaurant_a.admin.id,
                kind="asiento",
                occurred_at=_ago(800),
            )
        )
    await session.commit()

    report = await purge_once(_factory(session), NOW, PERIODS)

    assert (report.customers, report.activity_log) == (5, 5)


async def test_a_second_pass_changes_nothing(
    session: AsyncSession, restaurant_a: StaffedRestaurant, caplog: pytest.LogCaptureFixture
) -> None:
    customer = await _customer(session, restaurant_a, created_days_ago=900)
    await _order(session, restaurant_a, closed_days_ago=800, customer_id=customer)
    await _reservation(session, restaurant_a, days_ago=200)
    await _deactivate(session, restaurant_a.waiter.id or 0, days_ago=400)
    await session.commit()
    clock = FixedClock(NOW)

    first = await run_once(_factory(session), clock, PERIODS)
    second = await run_once(_factory(session), clock, PERIODS)

    assert (first.customers, first.orders, first.reservations, first.staff) == (1, 1, 1, 1)
    assert second == PurgeReport()
    # Solo la pasada que cambió algo queda en el log, con cuántas filas y no cuáles.
    (event,) = logged_events(caplog, "retention.purged")
    assert event["customers"] == 1
    assert event["staff"] == 1


def test_the_periods_by_default() -> None:
    assert (
        RetentionPeriods(
            customers=timedelta(days=730),
            order_contacts=timedelta(days=90),
            reservation_contacts=timedelta(days=90),
            staff=timedelta(days=365),
            audit_logs=timedelta(days=731),
        )
        == PERIODS
    )


def test_the_audit_period_cannot_go_below_the_database_floor() -> None:
    with pytest.raises(ValueError, match="731"):
        Settings(audit_log_retention_days=730)


async def test_the_loop_runs_and_stops(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    passes: list[datetime] = []
    second_pass = asyncio.Event()

    async def counting(*_args: object) -> PurgeReport:
        passes.append(NOW)
        if len(passes) == 1:
            message = "falla una vez"
            raise RuntimeError(message)
        second_pass.set()
        return PurgeReport()

    monkeypatch.setattr(data_retention, "run_once", counting)
    monkeypatch.setattr(data_retention, "_pause", _no_wait)
    loop = DataRetentionLoop(
        _factory(session), FixedClock(NOW), Settings(data_retention_interval_seconds=1)
    )

    loop.start()
    # Una pasada que falla no detiene las siguientes.
    await asyncio.wait_for(second_pass.wait(), timeout=5)
    await loop.stop()

    assert len(passes) >= 2


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


async def test_an_interval_of_zero_turns_it_off(session: AsyncSession) -> None:
    loop = DataRetentionLoop(
        _factory(session), FixedClock(NOW), Settings(data_retention_interval_seconds=0)
    )

    loop.start()
    await loop.stop()

    assert loop._task is None
