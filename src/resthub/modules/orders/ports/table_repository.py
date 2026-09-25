"""Puerto de persistencia de las mesas."""

from __future__ import annotations

from typing import Protocol

from resthub.modules.orders.domain.tables import DiningTable


class TableRepository(Protocol):
    async def add(self, table: DiningTable) -> DiningTable: ...

    async def get(self, restaurant_id: int, table_id: int) -> DiningTable | None: ...

    async def find_by_label(self, restaurant_id: int, label: str) -> DiningTable | None:
        """Sin distinguir mayúsculas: "Terraza" y "terraza" son la misma mesa."""
        ...

    async def list_all(self, restaurant_id: int) -> list[DiningTable]:
        """Todas, activas o no, por posición."""
        ...

    async def save(self, table: DiningTable) -> DiningTable: ...

    async def save_positions(self, tables: list[DiningTable]) -> None: ...
