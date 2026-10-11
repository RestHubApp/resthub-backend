"""Errores de dominio de reservas. Los adaptadores los traducen a HTTP."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class ReservationsError(Exception):
    """Raíz de los errores del módulo de reservas."""

    code: ClassVar[ErrorCode]


class InvalidReservationError(ReservationsError):
    code = ErrorCode.INVALID_RESERVATION

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReservationNotFoundError(ReservationsError):
    """También cuando es de otro restaurante."""

    code = ErrorCode.RESERVATION_NOT_FOUND

    def __init__(self, reservation_id: int) -> None:
        super().__init__(f"No existe la reserva {reservation_id}.")
        self.reservation_id = reservation_id


class TableNotFoundError(ReservationsError):
    code = ErrorCode.TABLE_NOT_FOUND

    def __init__(self, table_id: int) -> None:
        super().__init__(f"No existe la mesa {table_id}.")
        self.table_id = table_id


class CustomerNotFoundError(ReservationsError):
    """También cuando el cliente es de otro restaurante."""

    code = ErrorCode.CUSTOMER_NOT_FOUND

    def __init__(self, customer_id: int) -> None:
        super().__init__(f"No existe el cliente {customer_id}.")
        self.customer_id = customer_id


class ReservationConflictError(ReservationsError):
    code = ErrorCode.RESERVATION_CONFLICT

    def __init__(self, name: str, when: datetime) -> None:
        super().__init__(f"Esa mesa ya está reservada para {name} en ese horario.")
        self.name = name
        self.when = when
