"""Puerto de persistencia de los pedidos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.modules.orders.domain.orders import Order, OrderStatus, OrderType


@dataclass(frozen=True, slots=True)
class OrderQuery:
    # Obligatorio y primero: una búsqueda sin restaurante no se puede escribir.
    restaurant_id: int
    statuses: frozenset[OrderStatus] | None = None
    # Días del restaurante, ambos incluidos.
    date_from: date | None = None
    date_to: date | None = None
    type: OrderType | None = None
    table_id: int | None = None
    waiter_id: int | None = None
    # Con valor, solo los pedidos de esa cuenta más los activos de cualquiera:
    # lo que ve un mesero, que puede cubrir la mesa de un compañero pero no
    # revisar lo que cobró otro.
    visible_to: int | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class OrderRepository(Protocol):
    async def add(self, order: Order) -> Order:
        """Guarda un pedido nuevo; `OrderNumberTaken` si el número ya se usó ese día."""
        ...

    async def get(
        self, restaurant_id: int, order_id: int, *, for_update: bool = False
    ) -> Order | None:
        """El pedido; con `for_update`, tomado hasta el fin de la transacción.

        Todo caso de uso que cambia un pedido lo pide tomado, así dos cambios
        simultáneos (un cobro y un plato agregado) no se pisan.
        """
        ...

    async def by_client_request(self, restaurant_id: int, client_request_id: str) -> Order | None:
        """El pedido que ya abrió ese celular con ese identificador, si hay."""
        ...

    async def save(self, order: Order) -> Order: ...

    async def save_merge(self, target: Order, source: Order) -> Order:
        """Guarda dos mesas unidas: los ítems de `source` pasan a `target`."""
        ...

    async def search(self, query: OrderQuery) -> Page[Order]: ...

    async def list_active(self, restaurant_id: int) -> list[Order]:
        """Los pedidos activos, del más antiguo al más nuevo: el orden de la cocina."""
        ...

    async def active_for_table(self, restaurant_id: int, table_id: int) -> Order | None: ...

    async def last_number(self, restaurant_id: int, business_date: date) -> int:
        """El mayor número usado ese día; 0 si todavía no hubo pedidos."""
        ...
