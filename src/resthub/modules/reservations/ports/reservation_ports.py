"""Puertos de reservas."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from resthub.modules.reservations.domain.reservations import Reservation


class ReservationRepository(Protocol):
    async def add(self, reservation: Reservation) -> Reservation: ...

    async def get(
        self, restaurant_id: int, reservation_id: int, *, for_update: bool = False
    ) -> Reservation | None:
        """Con `for_update`, la reserva queda tomada hasta el fin de la transacción."""
        ...

    async def save(self, reservation: Reservation) -> Reservation: ...

    async def between(
        self, restaurant_id: int, start: datetime, end: datetime
    ) -> list[Reservation]:
        """Las que empiezan en ese rango, por hora."""
        ...

    async def for_table(
        self, restaurant_id: int, table_id: int, start: datetime, end: datetime
    ) -> list[Reservation]:
        """Las de esa mesa que tocan ese rango; tomadas hasta el fin de la transacción."""
        ...


class LocalCalendar(Protocol):
    async def timezone(self, restaurant_id: int) -> str: ...

    async def table_label(self, restaurant_id: int, table_id: int) -> str | None:
        """La etiqueta de una mesa del local, o `None` si no existe ahí.

        La mesa queda tomada hasta el fin de la transacción: dos reservas para
        la misma mesa se comprueban de a una.
        """
        ...


class CustomerDirectory(Protocol):
    async def exists(self, restaurant_id: int, customer_id: int) -> bool:
        """Si el cliente es de ese restaurante."""
        ...
