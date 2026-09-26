"""Lector hacia la libreta de clientes que posee `customers`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class KnownCustomer:
    id: int
    name: str
    phone: str
    address: str
    reference: str


class CustomerDirectory(Protocol):
    async def get(self, restaurant_id: int, customer_id: int) -> KnownCustomer | None:
        """El cliente si existe en ese restaurante."""
        ...

    async def by_phone(self, restaurant_id: int, phone: str) -> KnownCustomer | None:
        """El cliente con ese teléfono, sin importar los espacios."""
        ...
