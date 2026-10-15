from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page
from resthub.core.timestamps import as_utc
from resthub.modules.inventory.adapters.persistence.models import (
    StockCountLineRow,
    StockCountRow,
    WasteApprovalRow,
)
from resthub.modules.inventory.domain.exceptions import (
    StockCountAlreadyOpenError,
    StockCountNotFoundError,
    WasteApprovalNotFoundError,
)
from resthub.modules.inventory.domain.packaging import EntryMode, QuantityEntry
from resthub.modules.inventory.domain.stock_counts import (
    StockCount,
    StockCountLine,
    StockCountScope,
    StockCountStatus,
)
from resthub.modules.inventory.domain.waste_approvals import WasteApproval, WasteApprovalStatus
from resthub.modules.inventory.ports.stock_count_repository import CountedLine, StockCountQuery
from resthub.modules.inventory.ports.waste_approval_repository import WasteApprovalQuery

# -- Conteos físicos --------------------------------------------------------------------


def _entry(row: StockCountLineRow) -> QuantityEntry | None:
    if row.entry_mode is None or row.entry_quantity is None:
        return None
    return QuantityEntry(
        mode=EntryMode(row.entry_mode),
        quantity=row.entry_quantity,
        full_packages=row.entry_full_packages or Decimal(0),
    )


def _count_line(row: StockCountLineRow) -> StockCountLine:
    return StockCountLine(
        id=row.id,
        ingredient_id=row.ingredient_id,
        opening_stock=row.opening_stock,
        expected=row.expected,
        counted=row.counted,
        entry=_entry(row),
        counted_by=row.counted_by,
        counted_at=as_utc(row.counted_at) if row.counted_at else None,
        difference=row.difference,
        unit_cost=row.unit_cost,
    )


def _copy_line(line: StockCountLine, row: StockCountLineRow) -> None:
    row.ingredient_id = line.ingredient_id
    row.opening_stock = line.opening_stock
    row.expected = line.expected
    row.counted = line.counted
    row.entry_mode = line.entry.mode.value if line.entry else None
    row.entry_quantity = line.entry.quantity if line.entry else None
    row.entry_full_packages = (
        line.entry.full_packages if line.entry and line.entry.mode is EntryMode.WEIGHT else None
    )
    row.counted_by = line.counted_by
    row.counted_at = line.counted_at
    row.difference = line.difference
    row.unit_cost = line.unit_cost


def _count(row: StockCountRow) -> StockCount:
    return StockCount(
        id=row.id,
        restaurant_id=row.restaurant_id,
        number=row.number,
        created_by=row.created_by,
        lines=[_count_line(line) for line in row.lines],
        scope=StockCountScope(row.scope),
        note=row.note,
        status=StockCountStatus(row.status),
        created_at=as_utc(row.created_at),
        closed_by=row.closed_by,
        closed_at=as_utc(row.closed_at) if row.closed_at else None,
    )


def _copy_count(count: StockCount, row: StockCountRow) -> None:
    row.restaurant_id = count.restaurant_id
    row.number = count.number
    row.status = count.status.value
    row.scope = count.scope.value
    row.note = count.note
    row.created_by = count.created_by
    row.created_at = count.created_at
    row.closed_by = count.closed_by
    row.closed_at = count.closed_at


class SqlAlchemyStockCountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, count: StockCount) -> StockCount:
        lines = []
        for line in count.lines:
            line_row = StockCountLineRow(restaurant_id=count.restaurant_id)
            _copy_line(line, line_row)
            lines.append(line_row)
        row = StockCountRow(lines=lines)
        _copy_count(count, row)
        try:
            # En un punto de guardado: si otro abrió un conteo a la vez, se
            # deshace solo esto y la petición responde 409, no 500.
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            # Choca con el índice de un solo conteo en curso, o con el
            # correlativo de otro que se abrió en el mismo instante.
            raise StockCountAlreadyOpenError from error
        return _count(row)

    async def get(
        self, restaurant_id: int, count_id: int, *, for_update: bool = False
    ) -> StockCount | None:
        row = await self._row(restaurant_id, count_id, for_update=for_update)
        return _count(row) if row else None

    async def open_count(self, restaurant_id: int) -> StockCount | None:
        row = await self._session.scalar(
            select(StockCountRow).where(
                StockCountRow.restaurant_id == restaurant_id,
                StockCountRow.status == StockCountStatus.DRAFT.value,
            )
        )
        return _count(row) if row else None

    async def save(self, count: StockCount) -> StockCount:
        row = await self._row(count.restaurant_id, count.id or 0)
        if row is None:
            raise StockCountNotFoundError(count.id or 0)
        _copy_count(count, row)
        # Las líneas se fijan al abrir: solo cambia lo contado en cada una.
        by_id = {line.id: line for line in count.lines}
        for line_row in row.lines:
            line = by_id.get(line_row.id)
            if line is not None:
                _copy_line(line, line_row)
        await self._session.flush()
        return _count(row)

    async def search(self, query: StockCountQuery) -> Page[StockCount]:
        base = select(StockCountRow).where(StockCountRow.restaurant_id == query.restaurant_id)
        if query.status is not None:
            base = base.where(StockCountRow.status == query.status.value)
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        rows = await self._session.scalars(
            base.order_by(StockCountRow.number.desc()).limit(query.limit).offset(query.offset)
        )
        return Page(items=[_count(row) for row in rows], total=total)

    async def next_number(self, restaurant_id: int) -> int:
        last = await self._session.scalar(
            select(func.max(StockCountRow.number)).where(
                StockCountRow.restaurant_id == restaurant_id
            )
        )
        return int(last or 0) + 1

    async def counted_lines(
        self, restaurant_id: int, ingredient_id: int, limit: int
    ) -> list[CountedLine]:
        result = await self._session.execute(
            select(StockCountLineRow, StockCountRow.number, StockCountRow.closed_at)
            .join(StockCountRow, StockCountRow.id == StockCountLineRow.stock_count_id)
            .where(
                StockCountLineRow.restaurant_id == restaurant_id,
                StockCountLineRow.ingredient_id == ingredient_id,
                StockCountLineRow.counted.is_not(None),
                StockCountRow.status == StockCountStatus.CLOSED.value,
            )
            .order_by(StockCountRow.number.desc())
            .limit(limit)
        )
        return [
            CountedLine(
                count_id=line_row.stock_count_id,
                count_number=number,
                closed_at=as_utc(closed_at),
                line=_count_line(line_row),
            )
            for line_row, number, closed_at in result.all()
        ]

    async def _row(
        self, restaurant_id: int, count_id: int, *, for_update: bool = False
    ) -> StockCountRow | None:
        statement = select(StockCountRow).where(
            StockCountRow.id == count_id, StockCountRow.restaurant_id == restaurant_id
        )
        if for_update:
            # Dos cierres a la vez no ajustan el stock dos veces.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(statement)).scalar_one_or_none()


# -- Mermas por aprobar -----------------------------------------------------------------


def _approval(row: WasteApprovalRow) -> WasteApproval:
    return WasteApproval(
        id=row.id,
        restaurant_id=row.restaurant_id,
        ingredient_id=row.ingredient_id,
        quantity=row.quantity,
        unit_cost=row.unit_cost,
        reason=row.reason,
        created_by=row.created_by,
        status=WasteApprovalStatus(row.status),
        created_at=as_utc(row.created_at),
        decided_by=row.decided_by,
        decided_at=as_utc(row.decided_at) if row.decided_at else None,
        rejection_reason=row.rejection_reason,
        movement_id=row.movement_id,
    )


def _copy_approval(approval: WasteApproval, row: WasteApprovalRow) -> None:
    row.restaurant_id = approval.restaurant_id
    row.ingredient_id = approval.ingredient_id
    row.quantity = approval.quantity
    row.unit_cost = approval.unit_cost
    row.reason = approval.reason
    row.status = approval.status.value
    row.created_by = approval.created_by
    row.created_at = approval.created_at
    row.decided_by = approval.decided_by
    row.decided_at = approval.decided_at
    row.rejection_reason = approval.rejection_reason
    row.movement_id = approval.movement_id


class SqlAlchemyWasteApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, approval: WasteApproval) -> WasteApproval:
        row = WasteApprovalRow()
        _copy_approval(approval, row)
        self._session.add(row)
        await self._session.flush()
        return _approval(row)

    async def get(
        self, restaurant_id: int, approval_id: int, *, for_update: bool = False
    ) -> WasteApproval | None:
        row = await self._row(restaurant_id, approval_id, for_update=for_update)
        return _approval(row) if row else None

    async def save(self, approval: WasteApproval) -> WasteApproval:
        row = await self._row(approval.restaurant_id, approval.id or 0)
        if row is None:
            raise WasteApprovalNotFoundError(approval.id or 0)
        _copy_approval(approval, row)
        await self._session.flush()
        return _approval(row)

    async def search(self, query: WasteApprovalQuery) -> Page[WasteApproval]:
        base = select(WasteApprovalRow).where(WasteApprovalRow.restaurant_id == query.restaurant_id)
        if query.status is not None:
            base = base.where(WasteApprovalRow.status == query.status.value)
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        rows = await self._session.scalars(
            base.order_by(WasteApprovalRow.created_at.desc(), WasteApprovalRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        return Page(items=[_approval(row) for row in rows], total=total)

    async def _row(
        self, restaurant_id: int, approval_id: int, *, for_update: bool = False
    ) -> WasteApprovalRow | None:
        statement = select(WasteApprovalRow).where(
            WasteApprovalRow.id == approval_id, WasteApprovalRow.restaurant_id == restaurant_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(statement)).scalar_one_or_none()
