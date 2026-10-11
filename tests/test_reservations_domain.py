"""Reglas de las reservas sin HTTP: validación, ciclo, cruce de horarios y casos de uso."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from resthub.core.activity import ActivityKind
from resthub.modules.reservations.domain.exceptions import (
    CustomerNotFoundError,
    InvalidReservationError,
    ReservationConflictError,
    ReservationNotFoundError,
    TableNotFoundError,
)
from resthub.modules.reservations.domain.reservations import (
    GRACE,
    MAX_DURATION,
    MAX_PARTY_SIZE,
    MIN_DURATION,
    Reservation,
    ReservationStatus,
    ensure_table_free,
)
from resthub.modules.reservations.use_cases.manage_reservations import (
    ChangeReservationStatus,
    ListReservationsOfDay,
    ReservationData,
    SaveReservation,
)

RESTAURANT_ID = 1
WAITER_ID = 5
OWNER_ID = 6
EVENING = datetime(2030, 3, 10, 20, 0, tzinfo=UTC)


def _reservation(**data: object) -> Reservation:
    fields: dict[str, object] = {
        "restaurant_id": RESTAURANT_ID,
        "customer_name": "Ana Torres",
        "party_size": 4,
        "reserved_for": EVENING,
        "created_by": WAITER_ID,
    }
    fields.update(data)
    return Reservation(**fields)  # type: ignore[arg-type]


# -- Dominio ------------------------------------------------------------------------


def test_statuses_have_label_and_only_two_hold_the_table() -> None:
    assert [s.label for s in ReservationStatus] == [
        "Reservada",
        "Llegaron",
        "Cancelada",
        "No vinieron",
    ]
    assert [s for s in ReservationStatus if s.holds_table] == [
        ReservationStatus.BOOKED,
        ReservationStatus.SEATED,
    ]


def test_reservation_cleans_texts_and_lasts_two_hours_by_default() -> None:
    reservation = _reservation(
        customer_name="  Ana   Torres ", phone=" 987 654 ", notes=" cumple  "
    )

    assert (reservation.customer_name, reservation.phone, reservation.notes) == (
        "Ana Torres",
        "987 654",
        "cumple",
    )
    assert reservation.ends_at == EVENING + timedelta(minutes=120)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"customer_name": "  "}, "La reserva necesita el nombre de quien reserva."),
        ({"customer_name": "N" * 81}, "El nombre admite 80 caracteres como máximo."),
        ({"phone": "9" * 21}, "El teléfono admite 20 caracteres como máximo."),
        ({"notes": "N" * 301}, "La nota admite 300 caracteres como máximo."),
        ({"party_size": 0}, "Una reserva es de 1 a 50 personas."),
        ({"party_size": MAX_PARTY_SIZE + 1}, "Una reserva es de 1 a 50 personas."),
        ({"duration_minutes": MIN_DURATION - 1}, "La mesa se reserva de 30 a 360 minutos."),
        ({"duration_minutes": MAX_DURATION + 1}, "La mesa se reserva de 30 a 360 minutos."),
        (
            {"reserved_for": datetime(2030, 3, 10, 20, 0)},  # noqa: DTZ001 - la hora sin zona es el caso
            "La hora de la reserva necesita su zona horaria.",
        ),
    ],
)
def test_invalid_reservation_explains_reason(data: dict[str, object], message: str) -> None:
    with pytest.raises(InvalidReservationError) as error:
        _reservation(**data)
    assert error.value.reason == message


def test_exact_limits_are_accepted() -> None:
    assert _reservation(party_size=1, duration_minutes=MIN_DURATION).ends_at == EVENING + timedelta(
        minutes=30
    )
    assert _reservation(party_size=MAX_PARTY_SIZE, duration_minutes=MAX_DURATION).party_size == 50
    assert _reservation(customer_name="N" * 80, phone="9" * 20, notes="N" * 300).phone == "9" * 20


def test_accepted_with_fifteen_minutes_late_and_no_more() -> None:
    reservation = _reservation()

    reservation.ensure_future(EVENING + GRACE)
    with pytest.raises(InvalidReservationError) as error:
        reservation.ensure_future(EVENING + GRACE + timedelta(seconds=1))
    assert error.value.reason == "La hora de la reserva ya pasó."


def test_two_time_slots_overlap_only_if_they_intersect() -> None:
    reservation = _reservation()
    just_after = _reservation(reserved_for=reservation.ends_at)
    just_before = _reservation(reserved_for=EVENING - timedelta(minutes=120))
    overlapping = _reservation(reserved_for=reservation.ends_at - timedelta(minutes=1))

    assert not reservation.overlaps(just_after)
    assert not just_after.overlaps(reservation)
    assert not reservation.overlaps(just_before)
    assert reservation.overlaps(overlapping)
    assert overlapping.overlaps(reservation)


@pytest.mark.parametrize(
    "final", [ReservationStatus.SEATED, ReservationStatus.CANCELLED, ReservationStatus.NO_SHOW]
)
def test_closed_reservation_no_longer_changes_or_is_edited(final: ReservationStatus) -> None:
    reservation = _reservation()
    reservation.change_status(final)

    with pytest.raises(InvalidReservationError) as change_error:
        reservation.change_status(ReservationStatus.CANCELLED)
    with pytest.raises(InvalidReservationError) as edit_error:
        reservation.ensure_editable()
    assert change_error.value.reason == f"Una reserva «{final.label}» ya no cambia."
    assert edit_error.value.reason == f"Una reserva «{final.label}» ya no se edita."
    assert reservation.status is final


def test_reservation_does_not_return_to_booked() -> None:
    reservation = _reservation()

    with pytest.raises(InvalidReservationError) as error:
        reservation.change_status(ReservationStatus.BOOKED)
    assert error.value.reason == "La reserva ya está reservada."
    reservation.ensure_editable()


def test_free_table_ignores_own_reservation_other_tables_and_closed_ones() -> None:
    candidate = _reservation(id=1, table_id=3)
    same = _reservation(id=1, table_id=3)
    other_table = _reservation(id=2, table_id=4)
    cancelled = _reservation(id=3, table_id=3, status=ReservationStatus.CANCELLED)
    no_show = _reservation(id=4, table_id=3, status=ReservationStatus.NO_SHOW)
    later = _reservation(id=5, table_id=3, reserved_for=candidate.ends_at)

    ensure_table_free(candidate, [same, other_table, cancelled, no_show, later])


@pytest.mark.parametrize("status", [ReservationStatus.BOOKED, ReservationStatus.SEATED])
def test_active_reservation_on_same_table_conflicts(status: ReservationStatus) -> None:
    candidate = _reservation(table_id=3)
    active = _reservation(id=9, table_id=3, customer_name="Luis", status=status)

    with pytest.raises(ReservationConflictError) as error:
        ensure_table_free(candidate, [active])
    assert str(error.value) == "Esa mesa ya está reservada para Luis en ese horario."


def test_without_assigned_table_no_conflict_possible() -> None:
    ensure_table_free(_reservation(), [_reservation(id=9, table_id=3), _reservation(id=10)])


# -- Casos de uso con dobles en memoria --------------------------------------------------


class Schedule:
    def __init__(self) -> None:
        self.rows: dict[int, Reservation] = {}
        self.locks: list[int] = []
        self.windows: list[tuple[datetime, datetime]] = []

    async def add(self, reservation: Reservation) -> Reservation:
        stored = replace(reservation, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return replace(stored)

    async def get(
        self, restaurant_id: int, reservation_id: int, *, for_update: bool = False
    ) -> Reservation | None:
        if for_update:
            self.locks.append(reservation_id)
        found = self.rows.get(reservation_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def save(self, reservation: Reservation) -> Reservation:
        self.rows[reservation.id or 0] = replace(reservation)
        return replace(reservation)

    async def between(
        self, restaurant_id: int, start: datetime, end: datetime
    ) -> list[Reservation]:
        self.windows.append((start, end))
        return [
            replace(r)
            for r in self.rows.values()
            if r.restaurant_id == restaurant_id and start <= r.reserved_for < end
        ]

    async def for_table(
        self, restaurant_id: int, table_id: int, start: datetime, end: datetime
    ) -> list[Reservation]:
        self.windows.append((start, end))
        return [
            replace(r)
            for r in self.rows.values()
            if r.restaurant_id == restaurant_id and r.table_id == table_id
        ]


class Calendar:
    def __init__(self, tz_name: str = "America/Lima", tables: dict[int, str] | None = None) -> None:
        self.tz_name = tz_name
        self.tables = tables or {3: "Terraza 3"}

    async def timezone(self, restaurant_id: int) -> str:
        return self.tz_name

    async def table_label(self, restaurant_id: int, table_id: int) -> str | None:
        return self.tables.get(table_id)


class Customers:
    def __init__(self, *ids: int) -> None:
        self.ids = set(ids)

    async def exists(self, restaurant_id: int, customer_id: int) -> bool:
        return customer_id in self.ids


class ActivityLog:
    def __init__(self) -> None:
        self.entries: list[tuple[int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((user_id, kind, detail))


def _make_save(schedule: Schedule, activity_log: ActivityLog, *customers: int) -> SaveReservation:
    return SaveReservation(schedule, Calendar(), Customers(*customers), activity_log)


async def test_taking_reservation_with_table_logs_it_with_table_name() -> None:
    schedule, activity_log = Schedule(), ActivityLog()

    saved = await _make_save(schedule, activity_log)(
        RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 4, EVENING, table_id=3)
    )

    assert saved.id == 1
    assert saved.created_by == WAITER_ID
    assert activity_log.entries == [
        (WAITER_ID, ActivityKind.RESERVATION_CREATED, "Ana, 4 personas en Terraza 3")
    ]
    assert schedule.windows == [(EVENING - timedelta(hours=6), EVENING + timedelta(hours=2))]


async def test_reservation_without_table_names_no_table_in_log() -> None:
    schedule, activity_log = Schedule(), ActivityLog()

    await _make_save(schedule, activity_log)(
        RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 2, EVENING)
    )

    assert activity_log.entries[0][2] == "Ana, 2 personas"
    assert schedule.windows == []


async def test_editing_keeps_who_took_it_and_when() -> None:
    schedule, activity_log = Schedule(), ActivityLog()
    save_reservation = _make_save(schedule, activity_log, 7)
    created = await save_reservation(
        RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 4, EVENING, table_id=3)
    )

    edited = await save_reservation(
        RESTAURANT_ID,
        OWNER_ID,
        ReservationData("Ana", 6, EVENING, table_id=3, customer_id=7),
        created.id,
    )

    assert edited.id == created.id
    assert edited.party_size == 6
    assert edited.created_by == WAITER_ID
    assert edited.created_at == created.created_at
    assert schedule.locks == [created.id]
    assert activity_log.entries[-1] == (
        OWNER_ID,
        ActivityKind.RESERVATION_UPDATED,
        "Ana, 6 personas en Terraza 3",
    )


async def test_time_is_stored_in_utc() -> None:
    schedule, activity_log = Schedule(), ActivityLog()
    lima = datetime.fromisoformat("2030-03-10T15:00:00-05:00")

    saved = await _make_save(schedule, activity_log)(
        RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 2, lima)
    )

    assert saved.reserved_for == EVENING
    assert saved.reserved_for.tzinfo is UTC


async def test_closed_or_foreign_reservation_is_not_edited() -> None:
    schedule, activity_log = Schedule(), ActivityLog()
    save_reservation = _make_save(schedule, activity_log)
    created = await save_reservation(RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 4, EVENING))
    await ChangeReservationStatus(schedule, activity_log)(
        RESTAURANT_ID, WAITER_ID, created.id or 0, ReservationStatus.SEATED
    )

    with pytest.raises(InvalidReservationError):
        await save_reservation(
            RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 8, EVENING), created.id
        )
    with pytest.raises(ReservationNotFoundError):
        await save_reservation(2, WAITER_ID, ReservationData("Ana", 8, EVENING), created.id)
    assert schedule.rows[created.id or 0].party_size == 4


async def test_customer_or_table_missing_in_restaurant_is_rejected() -> None:
    schedule, activity_log = Schedule(), ActivityLog()
    save_reservation = _make_save(schedule, activity_log, 7)

    with pytest.raises(CustomerNotFoundError):
        await save_reservation(
            RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 4, EVENING, customer_id=8)
        )
    with pytest.raises(TableNotFoundError):
        await save_reservation(
            RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 4, EVENING, table_id=99)
        )
    assert schedule.rows == {}
    assert activity_log.entries == []


async def test_reservation_in_the_past_is_not_taken() -> None:
    schedule, activity_log = Schedule(), ActivityLog()

    with pytest.raises(InvalidReservationError):
        await _make_save(schedule, activity_log)(
            RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 4, datetime(2020, 1, 1, tzinfo=UTC))
        )


async def test_closing_reservation_takes_it_with_lock_and_logs_it() -> None:
    schedule, activity_log = Schedule(), ActivityLog()
    created = await _make_save(schedule, activity_log)(
        RESTAURANT_ID, WAITER_ID, ReservationData("Ana", 4, EVENING)
    )

    closed = await ChangeReservationStatus(schedule, activity_log)(
        RESTAURANT_ID, OWNER_ID, created.id or 0, ReservationStatus.NO_SHOW
    )

    assert closed.status is ReservationStatus.NO_SHOW
    assert schedule.rows[created.id or 0].status is ReservationStatus.NO_SHOW
    assert schedule.locks == [created.id]
    assert activity_log.entries[-1] == (
        OWNER_ID,
        ActivityKind.RESERVATION_UPDATED,
        "Ana: no vinieron",
    )


async def test_day_is_counted_in_restaurant_timezone() -> None:
    schedule = Schedule()

    await ListReservationsOfDay(schedule, Calendar("America/Lima"))(
        RESTAURANT_ID, date(2030, 3, 10)
    )

    assert schedule.windows == [
        (datetime(2030, 3, 10, 5, 0, tzinfo=UTC), datetime(2030, 3, 11, 5, 0, tzinfo=UTC))
    ]
