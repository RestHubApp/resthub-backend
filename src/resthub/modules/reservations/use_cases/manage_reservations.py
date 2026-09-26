"""Tomar, editar y cerrar reservas; ver las de un día.

Exige `reservations.manage` (mesero y encargado: cualquiera atiende el
teléfono) y `reservations.read` para verlas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.modules.reservations.domain.exceptions import (
    CustomerNotFound,
    ReservationNotFound,
    TableNotFound,
)
from resthub.modules.reservations.domain.reservations import (
    DEFAULT_DURATION,
    Reservation,
    ReservationStatus,
    ensure_table_free,
)
from resthub.modules.reservations.ports.reservation_ports import (
    CustomerDirectory,
    LocalCalendar,
    ReservationRepository,
)


@dataclass(frozen=True, slots=True)
class ReservationData:
    customer_name: str
    party_size: int
    reserved_for: datetime
    phone: str = ""
    table_id: int | None = None
    duration_minutes: int = DEFAULT_DURATION
    notes: str = ""
    customer_id: int | None = None


async def find_reservation(
    reservations: ReservationRepository,
    restaurant_id: int,
    reservation_id: int,
    *,
    for_update: bool = False,
) -> Reservation:
    reservation = await reservations.get(restaurant_id, reservation_id, for_update=for_update)
    if reservation is None:
        raise ReservationNotFound(reservation_id)
    return reservation


class SaveReservation:
    """Toma una reserva nueva o edita una vigente (con `reservation_id`)."""

    def __init__(
        self,
        reservations: ReservationRepository,
        calendar: LocalCalendar,
        customers: CustomerDirectory,
        activity: ActivityRecorder,
    ) -> None:
        self._reservations = reservations
        self._calendar = calendar
        self._customers = customers
        self._activity = activity

    async def __call__(
        self,
        restaurant_id: int,
        actor_id: int,
        data: ReservationData,
        reservation_id: int | None = None,
    ) -> Reservation:
        current = (
            await find_reservation(
                self._reservations, restaurant_id, reservation_id, for_update=True
            )
            if reservation_id is not None
            else None
        )
        if current is not None:
            current.ensure_editable()
        # La FK solo sabe que el cliente existe en algún local.
        if data.customer_id is not None and not await self._customers.exists(
            restaurant_id, data.customer_id
        ):
            raise CustomerNotFound(data.customer_id)
        candidate = Reservation(
            restaurant_id=restaurant_id,
            customer_name=data.customer_name,
            party_size=data.party_size,
            reserved_for=data.reserved_for.astimezone(UTC),
            created_by=current.created_by if current else actor_id,
            phone=data.phone,
            table_id=data.table_id,
            duration_minutes=data.duration_minutes,
            notes=data.notes,
            customer_id=data.customer_id,
            id=reservation_id,
        )
        candidate.ensure_future(datetime.now(UTC))
        label = await self._check_table(candidate)
        if current is None:
            saved = await self._reservations.add(candidate)
            kind = ActivityKind.RESERVATION_CREATED
        else:
            candidate.created_at = current.created_at
            saved = await self._reservations.save(candidate)
            kind = ActivityKind.RESERVATION_UPDATED
        await self._activity.record(
            restaurant_id,
            actor_id,
            kind,
            f"{saved.customer_name}, {saved.party_size} personas{f' en {label}' if label else ''}",
        )
        return saved

    async def _check_table(self, candidate: Reservation) -> str | None:
        if candidate.table_id is None:
            return None
        label = await self._calendar.table_label(candidate.restaurant_id, candidate.table_id)
        if label is None:
            raise TableNotFound(candidate.table_id)
        others = await self._reservations.for_table(
            candidate.restaurant_id,
            candidate.table_id,
            candidate.reserved_for - timedelta(hours=6),
            candidate.ends_at,
        )
        ensure_table_free(candidate, others)
        return label


class ChangeReservationStatus:
    """Llegaron, se canceló o no vinieron."""

    def __init__(self, reservations: ReservationRepository, activity: ActivityRecorder) -> None:
        self._reservations = reservations
        self._activity = activity

    async def __call__(
        self, restaurant_id: int, actor_id: int, reservation_id: int, status: ReservationStatus
    ) -> Reservation:
        # Tomada: dos cierres a la vez no pueden pasar los dos desde «reservada».
        reservation = await find_reservation(
            self._reservations, restaurant_id, reservation_id, for_update=True
        )
        reservation.change_status(status)
        saved = await self._reservations.save(reservation)
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.RESERVATION_UPDATED,
            f"{saved.customer_name}: {saved.status.label.lower()}",
        )
        return saved


class ListReservationsOfDay:
    """Las reservas de un día del restaurante, en su zona horaria."""

    def __init__(self, reservations: ReservationRepository, calendar: LocalCalendar) -> None:
        self._reservations = reservations
        self._calendar = calendar

    async def __call__(self, restaurant_id: int, day: date) -> list[Reservation]:
        zone = ZoneInfo(await self._calendar.timezone(restaurant_id))
        start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
        end = start + timedelta(days=1)
        return await self._reservations.between(restaurant_id, start, end)
