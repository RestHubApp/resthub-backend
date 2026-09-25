"""Adaptador de entrada HTTP de las mesas.

Verlas exige `tables.read` (los dos roles); crearlas, renombrarlas u ordenarlas,
`tables.manage`. Una mesa de otro restaurante responde 404.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.modules.orders.adapters.api.dependencies import (
    OrderRepositoryDep,
    StaffDirectoryDep,
    TableRepositoryDep,
)
from resthub.modules.orders.adapters.api.errors import http_error
from resthub.modules.orders.adapters.api.schemas import (
    CreateTableRequest,
    ReorderTablesRequest,
    TableResponse,
    TableStateResponse,
    UpdateTableRequest,
)
from resthub.modules.orders.domain.exceptions import OrdersError
from resthub.modules.orders.use_cases.manage_tables import (
    CreateTable,
    CreateTableCommand,
    ListTables,
    ListTablesQuery,
    ReorderTables,
    ReorderTablesCommand,
    UpdateTable,
    UpdateTableCommand,
)

router = APIRouter()

TableReaderDep = Annotated[Principal, Depends(require_permission(Permission.TABLES_READ))]
TableManagerDep = Annotated[Principal, Depends(require_permission(Permission.TABLES_MANAGE))]


@router.get("", response_model=list[TableStateResponse], summary="Mesas con su estado")
async def list_tables(
    principal: TableReaderDep,
    tables: TableRepositoryDep,
    orders: OrderRepositoryDep,
    staff: StaffDirectoryDep,
    include_inactive: Annotated[
        bool, Query(description="Incluye las desactivadas; solo con tables.manage")
    ] = False,
) -> list[TableStateResponse]:
    views = await ListTables(tables, orders, staff)(
        ListTablesQuery(
            restaurant_id=principal.restaurant_id,
            include_inactive=include_inactive and Permission.TABLES_MANAGE in principal.permissions,
        )
    )
    return [TableStateResponse.from_view(view) for view in views]


@router.post(
    "",
    response_model=TableResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agregar una mesa",
)
async def create_table(
    payload: CreateTableRequest,
    principal: TableManagerDep,
    tables: TableRepositoryDep,
    activity: ActivityRecorderDep,
) -> TableResponse:
    try:
        table = await CreateTable(tables, activity)(
            CreateTableCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                label=payload.label,
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return TableResponse.from_entity(table)


@router.put("/order", response_model=list[TableResponse], summary="Reordenar las mesas")
async def reorder_tables(
    payload: ReorderTablesRequest, principal: TableManagerDep, tables: TableRepositoryDep
) -> list[TableResponse]:
    try:
        ordered = await ReorderTables(tables)(
            ReorderTablesCommand(restaurant_id=principal.restaurant_id, table_ids=payload.ids)
        )
    except OrdersError as error:
        raise http_error(error) from error
    return [TableResponse.from_entity(table) for table in ordered]


@router.patch(
    "/{table_id}", response_model=TableResponse, summary="Renombrar o desactivar una mesa"
)
async def update_table(
    table_id: int,
    payload: UpdateTableRequest,
    principal: TableManagerDep,
    tables: TableRepositoryDep,
    activity: ActivityRecorderDep,
) -> TableResponse:
    try:
        table = await UpdateTable(tables, activity)(
            UpdateTableCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                table_id=table_id,
                label=payload.label,
                is_active=payload.is_active,
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return TableResponse.from_entity(table)
