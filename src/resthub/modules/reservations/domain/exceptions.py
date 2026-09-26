"""Errores de dominio de reservas. Los adaptadores los traducen a HTTP."""

from __future__ import annotations

from datetime import datetime


class ReservationsError(Exception):
    """Raíz de los errores del módulo de reservas."""


class InvalidReservation(ReservationsError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReservationNotFound(ReservationsError):
    """También cuando es de otro restaurante."""

    def __init__(self, reservation_id: int) -> None:
        super().__init__(f"No existe la reserva {reservation_id}.")
        self.reservation_id = reservation_id


class TableNotFound(ReservationsError):
    def __init__(self, table_id: int) -> None:
        super().__init__(f"No existe la mesa {table_id}.")
        self.table_id = table_id


class ReservationConflict(ReservationsError):
    def __init__(self, name: str, when: datetime) -> None:
        super().__init__(f"Esa mesa ya está reservada para {name} en ese horario.")
        self.name = name
        self.when = when
