"""Puertos de persistencia de proveedores y órdenes de compra."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from resthub.core.pagination import Page
from resthub.modules.inventory.domain.purchasing import (
    PurchaseOrder,
    PurchaseOrderStatus,
    Supplier,
)


class SupplierRepository(Protocol):
    async def add(self, supplier: Supplier) -> Supplier: ...

    async def get(self, restaurant_id: int, supplier_id: int) -> Supplier | None: ...

    async def find_by_name(self, restaurant_id: int, name: str) -> Supplier | None:
        """Sin distinguir mayúsculas."""
        ...

    async def list_all(self, restaurant_id: int) -> list[Supplier]: ...

    async def save(self, supplier: Supplier) -> Supplier: ...


@dataclass(frozen=True, slots=True)
class PurchaseOrderQuery:
    restaurant_id: int
    status: PurchaseOrderStatus | None = None
    supplier_id: int | None = None
    limit: int = 25
    offset: int = 0


class PurchaseOrderRepository(Protocol):
    async def add(self, order: PurchaseOrder) -> PurchaseOrder: ...

    async def get(
        self, restaurant_id: int, order_id: int, *, for_update: bool = False
    ) -> PurchaseOrder | None: ...

    async def save(self, order: PurchaseOrder) -> PurchaseOrder: ...

    async def search(self, query: PurchaseOrderQuery) -> Page[PurchaseOrder]:
        """De la más reciente a la más antigua."""
        ...

    async def next_number(self, restaurant_id: int) -> int: ...


@dataclass(frozen=True, slots=True)
class IngredientUsage:
    """Stock y consumo de un insumo activo, para sugerir cuánto comprar."""

    ingredient_id: int
    stock: Decimal
    min_stock: Decimal
    unit_cost: Decimal
    # Consumo más merma del período, en la unidad del insumo.
    used: Decimal


class UsageReader(Protocol):
    async def usage_since(self, restaurant_id: int, since: datetime) -> list[IngredientUsage]: ...
