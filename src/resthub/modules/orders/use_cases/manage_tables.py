"""Casos de uso de las mesas del salón."""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.modules.orders.domain.exceptions import TableLabelTaken, TableNotFound
from resthub.modules.orders.domain.orders import Order
from resthub.modules.orders.domain.tables import (
    DiningTable,
    TableStatus,
    reorder_tables,
    validate_label,
)
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.ports.staff_directory import StaffDirectory
from resthub.modules.orders.ports.table_repository import TableRepository


async def find_table(tables: TableRepository, restaurant_id: int, table_id: int) -> DiningTable:
    table = await tables.get(restaurant_id, table_id)
    if table is None:
        raise TableNotFound(table_id)
    return table


async def _ensure_label_is_free(
    tables: TableRepository, restaurant_id: int, label: str, own_id: int | None = None
) -> None:
    existing = await tables.find_by_label(restaurant_id, label)
    if existing is not None and existing.id != own_id:
        raise TableLabelTaken(label)


@dataclass(frozen=True, slots=True)
class TableView:
    table: DiningTable
    active_order: Order | None
    waiter_name: str = ""

    @property
    def status(self) -> TableStatus:
        return TableStatus.OCCUPIED if self.active_order is not None else TableStatus.FREE


@dataclass(frozen=True, slots=True)
class ListTablesQuery:
    restaurant_id: int
    include_inactive: bool = False


class ListTables:
    """Las mesas con su estado: libre, u ocupada y por qué pedido."""

    def __init__(
        self, tables: TableRepository, orders: OrderRepository, staff: StaffDirectory
    ) -> None:
        self._tables = tables
        self._orders = orders
        self._staff = staff

    async def __call__(self, query: ListTablesQuery) -> list[TableView]:
        tables = [
            table
            for table in await self._tables.list_all(query.restaurant_id)
            if query.include_inactive or table.is_active
        ]
        active = {
            order.table_id: order
            for order in await self._orders.list_active(query.restaurant_id)
            if order.table_id is not None
        }
        names = await self._staff.names(
            query.restaurant_id, {order.waiter_id for order in active.values()}
        )
        views: list[TableView] = []
        for table in tables:
            order = active.get(table.id)
            waiter_name = names.get(order.waiter_id, "") if order is not None else ""
            views.append(TableView(table=table, active_order=order, waiter_name=waiter_name))
        return views


@dataclass(frozen=True, slots=True)
class CreateTableCommand:
    restaurant_id: int
    actor_id: int
    label: str


class CreateTable:
    def __init__(self, tables: TableRepository, activity: ActivityRecorder) -> None:
        self._tables = tables
        self._activity = activity

    async def __call__(self, command: CreateTableCommand) -> DiningTable:
        label = validate_label(command.label)
        await _ensure_label_is_free(self._tables, command.restaurant_id, label)
        position = len(await self._tables.list_all(command.restaurant_id))
        created = await self._tables.add(
            DiningTable(restaurant_id=command.restaurant_id, label=label, position=position)
        )
        await self._activity.record(
            command.restaurant_id, command.actor_id, ActivityKind.TABLE_CREATED, created.label
        )
        return created


@dataclass(frozen=True, slots=True)
class UpdateTableCommand:
    restaurant_id: int
    actor_id: int
    table_id: int
    # `None` deja el campo como está.
    label: str | None = None
    is_active: bool | None = None


class UpdateTable:
    """Renombra o desactiva una mesa.

    Las mesas no se borran: los pedidos cobrados la siguen nombrando. Una mesa
    que se retira se desactiva y deja de ofrecerse para pedidos nuevos.
    """

    def __init__(self, tables: TableRepository, activity: ActivityRecorder) -> None:
        self._tables = tables
        self._activity = activity

    async def __call__(self, command: UpdateTableCommand) -> DiningTable:
        table = await find_table(self._tables, command.restaurant_id, command.table_id)
        if command.label is not None:
            label = validate_label(command.label)
            await _ensure_label_is_free(self._tables, command.restaurant_id, label, table.id)
            table.relabel(label)
        if command.is_active is not None:
            table.is_active = command.is_active
        saved = await self._tables.save(table)
        state = "activa" if saved.is_active else "inactiva"
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.TABLE_UPDATED,
            f"{saved.label} ({state})",
        )
        return saved


@dataclass(frozen=True, slots=True)
class ReorderTablesCommand:
    restaurant_id: int
    table_ids: list[int]


class ReorderTables:
    """El orden en que el celular del mesero muestra las mesas, como en el salón."""

    def __init__(self, tables: TableRepository) -> None:
        self._tables = tables

    async def __call__(self, command: ReorderTablesCommand) -> list[DiningTable]:
        ordered = reorder_tables(
            await self._tables.list_all(command.restaurant_id), command.table_ids
        )
        await self._tables.save_positions(ordered)
        return ordered
