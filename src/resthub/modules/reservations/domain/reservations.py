"""Reservas de mesa.

Python puro. Una reserva dice quién viene, cuántos, cuándo y, si se asignó,
en qué mesa. Ocupa la mesa un rato (dos horas por omisión): dos reservas
vigentes de la misma mesa no pueden cruzarse en el tiempo.

El ciclo es corto: reservada → llegaron (sentados), o cancelada, o no vino.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from resthub.core.clock import utc_now
from resthub.modules.reservations.domain.exceptions import (
    InvalidReservationError,
    ReservationConflictError,
)

MAX_NAME_LENGTH = 80
MAX_PHONE_LENGTH = 20
MAX_NOTES_LENGTH = 300
MAX_PARTY_SIZE = 50
DEFAULT_DURATION = 120
MIN_DURATION = 30
MAX_DURATION = 360
# Una reserva se puede cargar con hasta 15 minutos de atraso (el cliente llamó
# desde la puerta); antes de eso, ya pasó.
GRACE = timedelta(minutes=15)


class ReservationStatus(StrEnum):
    BOOKED = "booked"
    SEATED = "seated"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"

    @property
    def label(self) -> str:
        return {
            "booked": "Reservada",
            "seated": "Llegaron",
            "cancelled": "Cancelada",
            "no_show": "No vinieron",
        }[self.value]

    @property
    def holds_table(self) -> bool:
        return self in (ReservationStatus.BOOKED, ReservationStatus.SEATED)


def _text(raw: str, limit: int, what: str) -> str:
    text = " ".join(raw.split())
    if len(text) > limit:
        message = f"{what} admite {limit} caracteres como máximo."
        raise InvalidReservationError(message)
    return text


@dataclass(slots=True)
class Reservation:
    restaurant_id: int
    customer_name: str
    party_size: int
    reserved_for: datetime
    created_by: int
    phone: str = ""
    table_id: int | None = None
    duration_minutes: int = DEFAULT_DURATION
    notes: str = ""
    customer_id: int | None = None
    status: ReservationStatus = ReservationStatus.BOOKED
    id: int | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.customer_name = _text(self.customer_name, MAX_NAME_LENGTH, "El nombre")
        if not self.customer_name:
            message = "La reserva necesita el nombre de quien reserva."
            raise InvalidReservationError(message)
        self.phone = _text(self.phone, MAX_PHONE_LENGTH, "El teléfono")
        self.notes = _text(self.notes, MAX_NOTES_LENGTH, "La nota")
        if not 1 <= self.party_size <= MAX_PARTY_SIZE:
            message = f"Una reserva es de 1 a {MAX_PARTY_SIZE} personas."
            raise InvalidReservationError(message)
        if not MIN_DURATION <= self.duration_minutes <= MAX_DURATION:
            message = f"La mesa se reserva de {MIN_DURATION} a {MAX_DURATION} minutos."
            raise InvalidReservationError(message)
        if self.reserved_for.tzinfo is None:
            message = "La hora de la reserva necesita su zona horaria."
            raise InvalidReservationError(message)

    @property
    def ends_at(self) -> datetime:
        return self.reserved_for + timedelta(minutes=self.duration_minutes)

    def overlaps(self, other: Reservation) -> bool:
        return self.reserved_for < other.ends_at and other.reserved_for < self.ends_at

    def ensure_future(self, now: datetime) -> None:
        if self.reserved_for < now - GRACE:
            message = "La hora de la reserva ya pasó."
            raise InvalidReservationError(message)

    def change_status(self, status: ReservationStatus) -> None:
        if self.status is not ReservationStatus.BOOKED:
            message = f"Una reserva «{self.status.label}» ya no cambia."
            raise InvalidReservationError(message)
        if status is ReservationStatus.BOOKED:
            message = "La reserva ya está reservada."
            raise InvalidReservationError(message)
        self.status = status

    def ensure_editable(self) -> None:
        if self.status is not ReservationStatus.BOOKED:
            message = f"Una reserva «{self.status.label}» ya no se edita."
            raise InvalidReservationError(message)


def ensure_table_free(candidate: Reservation, others: list[Reservation]) -> None:
    """Dos reservas vigentes de la misma mesa no se cruzan en el tiempo."""
    if candidate.table_id is None:
        return
    for other in others:
        if (
            other.id != candidate.id
            and other.table_id == candidate.table_id
            and other.status.holds_table
            and candidate.overlaps(other)
        ):
            raise ReservationConflictError(other.customer_name, other.reserved_for)
