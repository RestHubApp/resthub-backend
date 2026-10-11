"""Cableado del adaptador HTTP de reservas."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.database import SessionDep
from resthub.modules.reservations.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyCustomerDirectory,
    SqlAlchemyLocalCalendarDirectory,
    SqlAlchemyReservationRepository,
)
from resthub.modules.reservations.ports.reservation_ports import (
    CustomerDirectory,
    LocalCalendarDirectory,
    ReservationRepository,
)


def get_reservation_repository(session: SessionDep) -> ReservationRepository:
    return SqlAlchemyReservationRepository(session)


def get_local_calendar(session: SessionDep) -> LocalCalendarDirectory:
    return SqlAlchemyLocalCalendarDirectory(session)


def get_customer_directory(session: SessionDep) -> CustomerDirectory:
    return SqlAlchemyCustomerDirectory(session)


ReservationRepositoryDep = Annotated[ReservationRepository, Depends(get_reservation_repository)]
LocalCalendarDep = Annotated[LocalCalendarDirectory, Depends(get_local_calendar)]
CustomerDirectoryDep = Annotated[CustomerDirectory, Depends(get_customer_directory)]
