"""Cableado del adaptador HTTP de reservas."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.auth import SessionDep
from resthub.modules.reservations.adapters.persistence.repositories import (
    SqlAlchemyReservationRepository,
    SqlCustomerDirectory,
    SqlLocalCalendar,
)
from resthub.modules.reservations.ports.reservation_ports import (
    CustomerDirectory,
    LocalCalendar,
    ReservationRepository,
)


def get_reservation_repository(session: SessionDep) -> ReservationRepository:
    return SqlAlchemyReservationRepository(session)


def get_local_calendar(session: SessionDep) -> LocalCalendar:
    return SqlLocalCalendar(session)


def get_customer_directory(session: SessionDep) -> CustomerDirectory:
    return SqlCustomerDirectory(session)


ReservationRepositoryDep = Annotated[ReservationRepository, Depends(get_reservation_repository)]
LocalCalendarDep = Annotated[LocalCalendar, Depends(get_local_calendar)]
CustomerDirectoryDep = Annotated[CustomerDirectory, Depends(get_customer_directory)]
