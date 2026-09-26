from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Integer, String, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.core.timestamps import as_utc
from resthub.modules.reservations.adapters.persistence.models import ReservationRow
from resthub.modules.reservations.domain.exceptions import ReservationNotFound
from resthub.modules.reservations.domain.reservations import (
    MAX_DURATION,
    Reservation,
    ReservationStatus,
)


def _reservation(row: ReservationRow) -> Reservation:
    return Reservation(
        id=row.id,
        restaurant_id=row.restaurant_id,
        customer_name=row.customer_name,
        phone=row.phone,
        party_size=row.party_size,
        reserved_for=as_utc(row.reserved_for),
        duration_minutes=row.duration_minutes,
        table_id=row.table_id,
        customer_id=row.customer_id,
        notes=row.notes,
        status=ReservationStatus(row.status),
        created_by=row.created_by,
        created_at=as_utc(row.created_at),
    )


def _copy(reservation: Reservation, row: ReservationRow) -> None:
    row.restaurant_id = reservation.restaurant_id
    row.customer_name = reservation.customer_name
    row.phone = reservation.phone
    row.party_size = reservation.party_size
    row.reserved_for = reservation.reserved_for
    row.duration_minutes = reservation.duration_minutes
    row.table_id = reservation.table_id
    row.customer_id = reservation.customer_id
    row.notes = reservation.notes
    row.status = reservation.status.value
    row.created_by = reservation.created_by
    row.created_at = reservation.created_at


class SqlAlchemyReservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, reservation: Reservation) -> Reservation:
        row = ReservationRow()
        _copy(reservation, row)
        self._session.add(row)
        await self._session.flush()
        return _reservation(row)

    async def get(
        self, restaurant_id: int, reservation_id: int, *, for_update: bool = False
    ) -> Reservation | None:
        row = await self._row(restaurant_id, reservation_id, for_update=for_update)
        return _reservation(row) if row else None

    async def save(self, reservation: Reservation) -> Reservation:
        row = await self._row(reservation.restaurant_id, reservation.id or 0)
        if row is None:
            raise ReservationNotFound(reservation.id or 0)
        _copy(reservation, row)
        await self._session.flush()
        return _reservation(row)

    async def between(
        self, restaurant_id: int, start: datetime, end: datetime
    ) -> list[Reservation]:
        rows = await self._session.scalars(
            select(ReservationRow)
            .where(
                ReservationRow.restaurant_id == restaurant_id,
                ReservationRow.reserved_for >= start,
                ReservationRow.reserved_for < end,
            )
            .order_by(ReservationRow.reserved_for, ReservationRow.id)
        )
        return [_reservation(row) for row in rows]

    async def for_table(
        self, restaurant_id: int, table_id: int, start: datetime, end: datetime
    ) -> list[Reservation]:
        # Una reserva que empezó hasta la duración máxima antes todavía puede cruzarse.
        rows = await self._session.scalars(
            select(ReservationRow)
            .where(
                ReservationRow.restaurant_id == restaurant_id,
                ReservationRow.table_id == table_id,
                ReservationRow.reserved_for >= start - timedelta(minutes=MAX_DURATION),
                ReservationRow.reserved_for < end,
            )
            .with_for_update()
        )
        return [_reservation(row) for row in rows]

    async def _row(
        self, restaurant_id: int, reservation_id: int, *, for_update: bool = False
    ) -> ReservationRow | None:
        statement = select(ReservationRow).where(
            ReservationRow.id == reservation_id,
            ReservationRow.restaurant_id == restaurant_id,
        )
        if for_update:
            # `populate_existing` descarta lo que la sesión tuviera en memoria.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(statement)).scalar_one_or_none()


_restaurants = table("restaurants", column("id", Integer), column("timezone", String))
_tables = table(
    "dining_tables",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("label", String),
)
_customers = table("customers", column("id", Integer), column("restaurant_id", Integer))


class SqlLocalCalendar:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def timezone(self, restaurant_id: int) -> str:
        zone = await self._session.scalar(
            select(_restaurants.c.timezone).where(_restaurants.c.id == restaurant_id)
        )
        return str(zone) if zone else DEFAULT_TIMEZONE

    async def table_label(self, restaurant_id: int, table_id: int) -> str | None:
        # Se toma la fila de la mesa y no las reservas: `FOR UPDATE` sobre las
        # reservas no bloquea nada cuando todavía no hay ninguna.
        label = await self._session.scalar(
            select(_tables.c.label)
            .where(_tables.c.id == table_id, _tables.c.restaurant_id == restaurant_id)
            .with_for_update()
        )
        return str(label) if label is not None else None


class SqlCustomerDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, restaurant_id: int, customer_id: int) -> bool:
        found = await self._session.scalar(
            select(_customers.c.id).where(
                _customers.c.id == customer_id, _customers.c.restaurant_id == restaurant_id
            )
        )
        return found is not None
