from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.orders.adapters.persistence.mappers import table_to_entity, table_to_row
from resthub.modules.orders.adapters.persistence.models import DiningTableRow
from resthub.modules.orders.domain.exceptions import TableNotFound
from resthub.modules.orders.domain.tables import DiningTable


class SqlAlchemyTableRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, table: DiningTable) -> DiningTable:
        row = table_to_row(table)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return table_to_entity(row)

    async def get(self, restaurant_id: int, table_id: int) -> DiningTable | None:
        row = await self._row(restaurant_id, table_id)
        return table_to_entity(row) if row else None

    async def find_by_label(self, restaurant_id: int, label: str) -> DiningTable | None:
        result = await self._session.execute(
            select(DiningTableRow).where(
                DiningTableRow.restaurant_id == restaurant_id,
                func.lower(DiningTableRow.label) == label.lower(),
            )
        )
        row = result.scalars().first()
        return table_to_entity(row) if row else None

    async def list_all(self, restaurant_id: int) -> list[DiningTable]:
        result = await self._session.execute(
            select(DiningTableRow)
            .where(DiningTableRow.restaurant_id == restaurant_id)
            .order_by(DiningTableRow.position, DiningTableRow.id)
        )
        return [table_to_entity(row) for row in result.scalars().all()]

    async def save(self, table: DiningTable) -> DiningTable:
        row = await self._row(table.restaurant_id, table.id or 0)
        if row is None:
            raise TableNotFound(table.id or 0)
        row.label = table.label
        row.position = table.position
        row.is_active = table.is_active
        await self._session.flush()
        return table_to_entity(row)

    async def save_positions(self, tables: list[DiningTable]) -> None:
        for table in tables:
            row = await self._row(table.restaurant_id, table.id or 0)
            if row is not None:
                row.position = table.position
        await self._session.flush()

    async def _row(self, restaurant_id: int, table_id: int) -> DiningTableRow | None:
        result = await self._session.execute(
            select(DiningTableRow).where(
                DiningTableRow.id == table_id, DiningTableRow.restaurant_id == restaurant_id
            )
        )
        return result.scalar_one_or_none()
