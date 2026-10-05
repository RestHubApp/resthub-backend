"""Puerto: lo que otros módulos guardan de un cliente.

Sus pedidos son de `orders` y sus reservas de `reservations`. Para los derechos
ARCO (Ley N.º 29733) hace falta mostrarlos y borrar de ellos sus datos, sin que
este módulo los importe: `wiring/customer_records.py` implementa el puerto y
`main.py` lo instala.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LinkedOrder:
    number: int
    type: str
    status: str
    total: Decimal
    created_at: datetime
    # Lo que quedó escrito en el pedido, que puede diferir de la ficha.
    customer_name: str
    customer_phone: str
    delivery_address: str
    delivery_reference: str
    notes: str


@dataclass(frozen=True, slots=True)
class LinkedReservation:
    reserved_for: datetime
    party_size: int
    status: str
    customer_name: str
    phone: str
    notes: str


class LinkedRecords(Protocol):
    async def has_active_orders(self, restaurant_id: int, customer_id: int) -> bool:
        """Pedidos que todavía no se cobran ni se cancelan."""
        ...

    async def orders(self, restaurant_id: int, customer_id: int) -> list[LinkedOrder]: ...

    async def reservations(
        self, restaurant_id: int, customer_id: int
    ) -> list[LinkedReservation]: ...

    async def anonymize(self, restaurant_id: int, customer_id: int, name: str) -> None:
        """Deja en sus pedidos y reservas `name` y borra teléfono, dirección y notas."""
        ...
