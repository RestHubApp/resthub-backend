"""Puertos de clientes: su libreta y lo que dicen sus pedidos."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from resthub.core.pagination import Page
from resthub.modules.customers.domain.customers import Customer, CustomerStats


@dataclass(frozen=True, slots=True)
class CustomerQuery:
    restaurant_id: int
    # Busca en nombre y teléfono.
    text: str = ""
    limit: int = 25
    offset: int = 0


class CustomerRepository(Protocol):
    async def add(self, customer: Customer) -> Customer: ...

    async def get(self, restaurant_id: int, customer_id: int) -> Customer | None: ...

    async def find_by_phone(self, restaurant_id: int, phone_key: str) -> Customer | None: ...

    async def save(self, customer: Customer) -> Customer: ...

    async def search(self, query: CustomerQuery) -> Page[Customer]:
        """Por nombre, del más reciente al más antiguo si no hay texto."""
        ...


@dataclass(frozen=True, slots=True)
class CustomerOrder:
    """Un pedido del cliente, leído de `orders`."""

    order_id: int
    number: int
    type: str
    status: str
    total: Decimal
    created_at: datetime


class CustomerHistory(Protocol):
    async def stats(
        self, restaurant_id: int, customer_ids: Collection[int]
    ) -> dict[int, CustomerStats]:
        """Visitas pagadas, gasto y última visita de cada cliente."""
        ...

    async def orders(self, restaurant_id: int, customer_id: int, limit: int) -> list[CustomerOrder]:
        """Sus últimos pedidos, del más reciente al más antiguo."""
        ...
