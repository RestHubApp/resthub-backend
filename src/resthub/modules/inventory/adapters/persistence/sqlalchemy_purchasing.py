from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page
from resthub.core.timestamps import as_utc
from resthub.modules.inventory.adapters.persistence.models import (
    IngredientRow,
    PurchaseOrderLineRow,
    PurchaseOrderRow,
    StockMovementRow,
    SupplierRow,
)
from resthub.modules.inventory.domain.entities import MovementKind
from resthub.modules.inventory.domain.exceptions import PurchaseOrderNotFound, SupplierNotFound
from resthub.modules.inventory.domain.purchasing import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Supplier,
)
from resthub.modules.inventory.ports.purchasing_repository import (
    IngredientUsage,
    PurchaseOrderQuery,
)


def _supplier(row: SupplierRow) -> Supplier:
    return Supplier(
        id=row.id,
        restaurant_id=row.restaurant_id,
        name=row.name,
        contact=row.contact,
        phone=row.phone,
        notes=row.notes,
        is_active=row.is_active,
        created_at=as_utc(row.created_at),
    )


def _copy_supplier(supplier: Supplier, row: SupplierRow) -> None:
    row.restaurant_id = supplier.restaurant_id
    row.name = supplier.name
    row.contact = supplier.contact
    row.phone = supplier.phone
    row.notes = supplier.notes
    row.is_active = supplier.is_active
    row.created_at = supplier.created_at


class SqlAlchemySupplierRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, supplier: Supplier) -> Supplier:
        row = SupplierRow()
        _copy_supplier(supplier, row)
        self._session.add(row)
        await self._session.flush()
        return _supplier(row)

    async def get(self, restaurant_id: int, supplier_id: int) -> Supplier | None:
        row = await self._row(restaurant_id, supplier_id)
        return _supplier(row) if row else None

    async def find_by_name(self, restaurant_id: int, name: str) -> Supplier | None:
        row = (
            await self._session.execute(
                select(SupplierRow).where(
                    SupplierRow.restaurant_id == restaurant_id,
                    func.lower(SupplierRow.name) == name.lower(),
                )
            )
        ).scalar_one_or_none()
        return _supplier(row) if row else None

    async def list_all(self, restaurant_id: int) -> list[Supplier]:
        rows = await self._session.scalars(
            select(SupplierRow)
            .where(SupplierRow.restaurant_id == restaurant_id)
            .order_by(SupplierRow.name)
        )
        return [_supplier(row) for row in rows]

    async def save(self, supplier: Supplier) -> Supplier:
        row = await self._row(supplier.restaurant_id, supplier.id or 0)
        if row is None:
            raise SupplierNotFound(supplier.id or 0)
        _copy_supplier(supplier, row)
        await self._session.flush()
        return _supplier(row)

    async def _row(self, restaurant_id: int, supplier_id: int) -> SupplierRow | None:
        return (
            await self._session.execute(
                select(SupplierRow).where(
                    SupplierRow.id == supplier_id, SupplierRow.restaurant_id == restaurant_id
                )
            )
        ).scalar_one_or_none()


def _line(row: PurchaseOrderLineRow) -> PurchaseOrderLine:
    return PurchaseOrderLine(
        id=row.id,
        ingredient_id=row.ingredient_id,
        quantity=row.quantity,
        unit_cost=row.unit_cost,
        received_quantity=row.received_quantity,
        received_unit_cost=row.received_unit_cost,
    )


def _order(row: PurchaseOrderRow) -> PurchaseOrder:
    return PurchaseOrder(
        id=row.id,
        restaurant_id=row.restaurant_id,
        number=row.number,
        supplier_id=row.supplier_id,
        created_by=row.created_by,
        lines=[_line(line) for line in row.lines],
        notes=row.notes,
        status=PurchaseOrderStatus(row.status),
        created_at=as_utc(row.created_at),
        sent_at=as_utc(row.sent_at) if row.sent_at else None,
        received_at=as_utc(row.received_at) if row.received_at else None,
        cancelled_at=as_utc(row.cancelled_at) if row.cancelled_at else None,
    )


def _copy_order(order: PurchaseOrder, row: PurchaseOrderRow) -> None:
    row.restaurant_id = order.restaurant_id
    row.number = order.number
    row.supplier_id = order.supplier_id
    row.created_by = order.created_by
    row.status = order.status.value
    row.notes = order.notes
    row.created_at = order.created_at
    row.sent_at = order.sent_at
    row.received_at = order.received_at
    row.cancelled_at = order.cancelled_at


def _line_row(line: PurchaseOrderLine, restaurant_id: int) -> PurchaseOrderLineRow:
    return PurchaseOrderLineRow(
        restaurant_id=restaurant_id,
        ingredient_id=line.ingredient_id,
        quantity=line.quantity,
        unit_cost=line.unit_cost,
        received_quantity=line.received_quantity,
        received_unit_cost=line.received_unit_cost,
    )


class SqlAlchemyPurchaseOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, order: PurchaseOrder) -> PurchaseOrder:
        row = PurchaseOrderRow(lines=[_line_row(line, order.restaurant_id) for line in order.lines])
        _copy_order(order, row)
        self._session.add(row)
        await self._session.flush()
        return _order(row)

    async def get(
        self, restaurant_id: int, order_id: int, *, for_update: bool = False
    ) -> PurchaseOrder | None:
        row = await self._row(restaurant_id, order_id, for_update=for_update)
        return _order(row) if row else None

    async def save(self, order: PurchaseOrder) -> PurchaseOrder:
        row = await self._row(order.restaurant_id, order.id or 0)
        if row is None:
            raise PurchaseOrderNotFound(order.id or 0)
        _copy_order(order, row)
        # Mientras es borrador las líneas se reemplazan enteras; después solo
        # cambia lo recibido en cada una.
        kept = {line.id: line for line in order.lines if line.id is not None}
        for line_row in list(row.lines):
            line = kept.get(line_row.id)
            if line is None:
                row.lines.remove(line_row)
                continue
            line_row.quantity = line.quantity
            line_row.unit_cost = line.unit_cost
            line_row.received_quantity = line.received_quantity
            line_row.received_unit_cost = line.received_unit_cost
        row.lines.extend(
            _line_row(line, order.restaurant_id) for line in order.lines if line.id is None
        )
        await self._session.flush()
        return _order(row)

    async def search(self, query: PurchaseOrderQuery) -> Page[PurchaseOrder]:
        base = select(PurchaseOrderRow).where(PurchaseOrderRow.restaurant_id == query.restaurant_id)
        if query.status is not None:
            base = base.where(PurchaseOrderRow.status == query.status.value)
        if query.supplier_id is not None:
            base = base.where(PurchaseOrderRow.supplier_id == query.supplier_id)
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        rows = await self._session.scalars(
            base.order_by(PurchaseOrderRow.number.desc()).limit(query.limit).offset(query.offset)
        )
        return Page(items=[_order(row) for row in rows], total=total)

    async def next_number(self, restaurant_id: int) -> int:
        last = await self._session.scalar(
            select(func.max(PurchaseOrderRow.number)).where(
                PurchaseOrderRow.restaurant_id == restaurant_id
            )
        )
        return int(last or 0) + 1

    async def _row(
        self, restaurant_id: int, order_id: int, *, for_update: bool = False
    ) -> PurchaseOrderRow | None:
        statement = select(PurchaseOrderRow).where(
            PurchaseOrderRow.id == order_id, PurchaseOrderRow.restaurant_id == restaurant_id
        )
        if for_update:
            # Dos personas recibiendo la misma orden no la cargan dos veces al stock.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(statement)).scalar_one_or_none()


class SqlUsageReader:
    """Stock y salida reciente de cada insumo activo, para sugerir compras."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def usage_since(self, restaurant_id: int, since: datetime) -> list[IngredientUsage]:
        outflow = (MovementKind.CONSUMPTION.value, MovementKind.WASTE.value)
        totals = (
            select(
                StockMovementRow.ingredient_id,
                func.sum(StockMovementRow.quantity).label("stock"),
                func.sum(
                    case(
                        (
                            (StockMovementRow.kind.in_(outflow))
                            & (StockMovementRow.created_at >= since),
                            -StockMovementRow.quantity,
                        ),
                        else_=0,
                    )
                ).label("used"),
            )
            .where(StockMovementRow.restaurant_id == restaurant_id)
            .group_by(StockMovementRow.ingredient_id)
            .subquery()
        )
        result = await self._session.execute(
            select(
                IngredientRow.id,
                IngredientRow.min_stock,
                IngredientRow.unit_cost,
                totals.c.stock,
                totals.c.used,
            )
            .outerjoin(totals, totals.c.ingredient_id == IngredientRow.id)
            .where(IngredientRow.restaurant_id == restaurant_id, IngredientRow.is_active.is_(True))
            .order_by(IngredientRow.name)
        )
        return [
            IngredientUsage(
                ingredient_id=row.id,
                stock=Decimal(str(row.stock or 0)),
                min_stock=row.min_stock,
                unit_cost=row.unit_cost,
                used=Decimal(str(row.used or 0)),
            )
            for row in result
        ]
