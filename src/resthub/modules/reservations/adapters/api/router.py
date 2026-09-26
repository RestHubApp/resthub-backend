"""Adaptador de entrada HTTP de reservas.

`reservations.read` para ver las del día; `reservations.manage` para tomar,
editar y cerrarlas. El encargado y el mesero los tienen de entrada: cualquiera
atiende el teléfono.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import SessionDep, require_permission
from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.modules.reservations.adapters.persistence.repositories import (
    SqlAlchemyReservationRepository,
    SqlCustomerDirectory,
    SqlLocalCalendar,
)
from resthub.modules.reservations.domain.exceptions import (
    CustomerNotFound,
    ReservationConflict,
    ReservationNotFound,
    ReservationsError,
    TableNotFound,
)
from resthub.modules.reservations.domain.reservations import (
    DEFAULT_DURATION,
    MAX_DURATION,
    MAX_NAME_LENGTH,
    MAX_NOTES_LENGTH,
    MAX_PARTY_SIZE,
    MAX_PHONE_LENGTH,
    MIN_DURATION,
    Reservation,
    ReservationStatus,
)
from resthub.modules.reservations.use_cases.manage_reservations import (
    ChangeReservationStatus,
    ListReservationsOfDay,
    ReservationData,
    SaveReservation,
)

router = APIRouter()

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.RESERVATIONS_READ))]
ManagerDep = Annotated[Principal, Depends(require_permission(Permission.RESERVATIONS_MANAGE))]


class ReservationRequest(BaseModel):
    customer_name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    party_size: int = Field(ge=1, le=MAX_PARTY_SIZE)
    # Con zona horaria: `2026-10-02T20:00:00-05:00`.
    reserved_for: datetime
    phone: str = Field(default="", max_length=MAX_PHONE_LENGTH)
    table_id: int | None = Field(default=None, ge=1)
    duration_minutes: int = Field(default=DEFAULT_DURATION, ge=MIN_DURATION, le=MAX_DURATION)
    notes: str = Field(default="", max_length=MAX_NOTES_LENGTH)
    customer_id: int | None = Field(default=None, ge=1)


class ReservationResponse(BaseModel):
    id: int
    customer_name: str
    phone: str
    party_size: int
    reserved_for: datetime
    ends_at: datetime
    duration_minutes: int
    table_id: int | None
    customer_id: int | None
    notes: str
    status: ReservationStatus
    status_label: str
    created_at: datetime

    @classmethod
    def from_entity(cls, reservation: Reservation) -> ReservationResponse:
        return cls(
            id=reservation.id or 0,
            customer_name=reservation.customer_name,
            phone=reservation.phone,
            party_size=reservation.party_size,
            reserved_for=reservation.reserved_for,
            ends_at=reservation.ends_at,
            duration_minutes=reservation.duration_minutes,
            table_id=reservation.table_id,
            customer_id=reservation.customer_id,
            notes=reservation.notes,
            status=reservation.status,
            status_label=reservation.status.label,
            created_at=reservation.created_at,
        )


def _http_error(error: ReservationsError) -> HTTPException:
    if isinstance(error, ReservationNotFound | TableNotFound | CustomerNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, ReservationConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


@router.get("", response_model=list[ReservationResponse], summary="Reservas de un día")
async def list_reservations(
    principal: ReaderDep,
    session: SessionDep,
    day: Annotated[date, Query(description="Día del restaurante")],
) -> list[ReservationResponse]:
    reservations = await ListReservationsOfDay(
        SqlAlchemyReservationRepository(session), SqlLocalCalendar(session)
    )(principal.restaurant_id, day)
    return [ReservationResponse.from_entity(r) for r in reservations]


@router.post(
    "",
    response_model=ReservationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Tomar una reserva",
)
async def create_reservation(
    payload: ReservationRequest,
    principal: ManagerDep,
    session: SessionDep,
    activity: ActivityRecorderDep,
) -> ReservationResponse:
    return await _save(payload, principal, session, activity, None)


@router.put("/{reservation_id}", response_model=ReservationResponse, summary="Editar")
async def update_reservation(
    reservation_id: int,
    payload: ReservationRequest,
    principal: ManagerDep,
    session: SessionDep,
    activity: ActivityRecorderDep,
) -> ReservationResponse:
    return await _save(payload, principal, session, activity, reservation_id)


async def _save(
    payload: ReservationRequest,
    principal: Principal,
    session: SessionDep,
    activity: ActivityRecorderDep,
    reservation_id: int | None,
) -> ReservationResponse:
    save = SaveReservation(
        SqlAlchemyReservationRepository(session),
        SqlLocalCalendar(session),
        SqlCustomerDirectory(session),
        activity,
    )
    try:
        if payload.reserved_for.tzinfo is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "La hora necesita su zona horaria."
            )
        saved = await save(
            principal.restaurant_id,
            principal.user_id,
            ReservationData(**payload.model_dump()),
            reservation_id,
        )
    except ReservationsError as error:
        raise _http_error(error) from error
    return ReservationResponse.from_entity(saved)


@router.post(
    "/{reservation_id}/status",
    response_model=ReservationResponse,
    summary="Llegaron, cancelada o no vinieron",
)
async def change_reservation_status(
    reservation_id: int,
    new_status: Annotated[ReservationStatus, Query(alias="value")],
    principal: ManagerDep,
    session: SessionDep,
    activity: ActivityRecorderDep,
) -> ReservationResponse:
    try:
        saved = await ChangeReservationStatus(SqlAlchemyReservationRepository(session), activity)(
            principal.restaurant_id, principal.user_id, reservation_id, new_status
        )
    except ReservationsError as error:
        raise _http_error(error) from error
    return ReservationResponse.from_entity(saved)
