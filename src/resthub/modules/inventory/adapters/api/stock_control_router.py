"""Adaptador de entrada HTTP de los conteos físicos y de las mermas por aprobar.

Cuelga del mismo prefijo `/inventory`. Leer exige `inventory.read`; abrir,
cargar, cerrar y cancelar un conteo, `inventory.manage`; aprobar o rechazar
una merma, `inventory.approve_waste`. Un conteo, una merma o un insumo de otro
restaurante responde 404.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.api_params import PathId
from resthub.core.auth import require_permission
from resthub.core.clock_dependency import ClockDep
from resthub.core.errors import api_error
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.modules.inventory.adapters.api.dependencies import (
    IngredientRepositoryDep,
    StaffDirectoryDep,
    StockCountRepositoryDep,
    StockLedgerDep,
    WasteApprovalRepositoryDep,
)
from resthub.modules.inventory.adapters.api.stock_control_schemas import (
    CountDifferencesResponse,
    IngredientCountHistoryResponse,
    OpenStockCountRequest,
    RecordCountLinesRequest,
    RejectWasteRequest,
    StockCountPageResponse,
    StockCountResponse,
    StockCountSummaryResponse,
    WasteApprovalPageResponse,
    WasteApprovalResponse,
)
from resthub.modules.inventory.domain.exceptions import (
    IngredientInactiveError,
    IngredientNotFoundError,
    InventoryError,
    StockCountAlreadyOpenError,
    StockCountNotClosedError,
    StockCountNotFoundError,
    StockCountNotOpenError,
    WasteAlreadyDecidedError,
    WasteApprovalNotFoundError,
)
from resthub.modules.inventory.domain.stock_counts import StockCount, StockCountStatus
from resthub.modules.inventory.domain.waste_approvals import WasteApproval, WasteApprovalStatus
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.staff_directory import StaffDirectory
from resthub.modules.inventory.ports.stock_count_repository import StockCountQuery
from resthub.modules.inventory.ports.waste_approval_repository import WasteApprovalQuery
from resthub.modules.inventory.use_cases.register_movements import RegisterWaste
from resthub.modules.inventory.use_cases.stock_counts import (
    DEFAULT_HISTORY_SIZE,
    CancelStockCount,
    CloseStockCount,
    CountEntry,
    IngredientCountHistory,
    ListStockCounts,
    OpenStockCount,
    OpenStockCountCommand,
    ReadCountDifferences,
    RecordCountLines,
    RecordCountLinesCommand,
    find_stock_count,
)
from resthub.modules.inventory.use_cases.waste_approvals import (
    ApproveWaste,
    ListWasteApprovals,
    RejectWaste,
)

router = APIRouter()

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.INVENTORY_READ))]
ManagerDep = Annotated[Principal, Depends(require_permission(Permission.INVENTORY_MANAGE))]
ApproverDep = Annotated[Principal, Depends(require_permission(Permission.INVENTORY_APPROVE_WASTE))]
# Quien aprueba necesita ver la lista aunque no tenga `inventory.read`.
ApprovalsReaderDep = Annotated[
    Principal,
    Depends(require_permission(Permission.INVENTORY_READ, Permission.INVENTORY_APPROVE_WASTE)),
]

MAX_HISTORY_SIZE = 50


def _http_error(error: InventoryError) -> HTTPException:
    if isinstance(
        error,
        StockCountNotFoundError | WasteApprovalNotFoundError | IngredientNotFoundError,
    ):
        return api_error(status.HTTP_404_NOT_FOUND, error)
    if isinstance(
        error,
        StockCountAlreadyOpenError
        | StockCountNotOpenError
        | StockCountNotClosedError
        | WasteAlreadyDecidedError
        | IngredientInactiveError,
    ):
        return api_error(status.HTTP_409_CONFLICT, error)
    return api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)


async def _count_response(
    count: StockCount, ingredients: IngredientRepository
) -> StockCountResponse:
    by_id = await ingredients.get_many(
        count.restaurant_id, [line.ingredient_id for line in count.lines]
    )
    return StockCountResponse.build(count, by_id)


async def _approval_responses(
    restaurant_id: int,
    approvals: list[WasteApproval],
    ingredients: IngredientRepository,
    staff: StaffDirectory,
) -> list[WasteApprovalResponse]:
    by_id = await ingredients.get_many(restaurant_id, {a.ingredient_id for a in approvals})
    people = {a.created_by for a in approvals} | {
        a.decided_by for a in approvals if a.decided_by is not None
    }
    names = await staff.names(restaurant_id, people)
    return [WasteApprovalResponse.build(approval, by_id, names) for approval in approvals]


# -- Conteos físicos --------------------------------------------------------------------


@router.get("/stock-counts", response_model=StockCountPageResponse, summary="Conteos físicos")
async def list_stock_counts(
    principal: ReaderDep,
    counts: StockCountRepositoryDep,
    count_status: Annotated[StockCountStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> StockCountPageResponse:
    page = await ListStockCounts(counts)(
        StockCountQuery(
            restaurant_id=principal.restaurant_id, status=count_status, limit=limit, offset=offset
        )
    )
    return StockCountPageResponse(
        items=[StockCountSummaryResponse.from_entity(count) for count in page.items],
        total=page.total,
    )


@router.post(
    "/stock-counts",
    response_model=StockCountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Abrir un conteo físico (toma la foto del stock)",
)
async def open_stock_count(
    payload: OpenStockCountRequest,
    principal: ManagerDep,
    counts: StockCountRepositoryDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> StockCountResponse:
    try:
        count = await OpenStockCount(counts, ingredients, ledger, activity, clock)(
            OpenStockCountCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                scope=payload.scope,
                ingredient_ids=payload.ingredient_ids,
                note=payload.note,
            )
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return await _count_response(count, ingredients)


@router.get(
    "/stock-counts/{count_id}", response_model=StockCountResponse, summary="Un conteo físico"
)
async def read_stock_count(
    count_id: PathId,
    principal: ReaderDep,
    counts: StockCountRepositoryDep,
    ingredients: IngredientRepositoryDep,
) -> StockCountResponse:
    try:
        count = await find_stock_count(counts, principal.restaurant_id, count_id)
    except InventoryError as error:
        raise _http_error(error) from error
    return await _count_response(count, ingredients)


@router.put(
    "/stock-counts/{count_id}/lines",
    response_model=StockCountResponse,
    summary="Cargar o corregir lo contado (en su unidad, en envases o por peso)",
)
async def record_count_lines(
    count_id: PathId,
    payload: RecordCountLinesRequest,
    principal: ManagerDep,
    counts: StockCountRepositoryDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    clock: ClockDep,
) -> StockCountResponse:
    try:
        count = await RecordCountLines(counts, ingredients, ledger, clock)(
            RecordCountLinesCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                count_id=count_id,
                entries=[
                    CountEntry(ingredient_id=line.ingredient_id, entry=line.to_entry())
                    for line in payload.lines
                ],
            )
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return await _count_response(count, ingredients)


@router.post(
    "/stock-counts/{count_id}/close",
    response_model=StockCountResponse,
    summary="Cerrar el conteo: cada diferencia entra al stock como un ajuste",
)
async def close_stock_count(
    count_id: PathId,
    principal: ManagerDep,
    counts: StockCountRepositoryDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> StockCountResponse:
    try:
        count = await CloseStockCount(counts, ingredients, ledger, activity, clock)(
            principal.restaurant_id, principal.user_id, count_id
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return await _count_response(count, ingredients)


@router.post(
    "/stock-counts/{count_id}/cancel",
    response_model=StockCountResponse,
    summary="Cancelar el conteo sin tocar el stock",
)
async def cancel_stock_count(
    count_id: PathId,
    principal: ManagerDep,
    counts: StockCountRepositoryDep,
    ingredients: IngredientRepositoryDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> StockCountResponse:
    try:
        count = await CancelStockCount(counts, activity, clock)(
            principal.restaurant_id, principal.user_id, count_id
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return await _count_response(count, ingredients)


@router.get(
    "/stock-counts/{count_id}/differences",
    response_model=CountDifferencesResponse,
    summary="Diferencias valorizadas de un conteo cerrado, de la mayor pérdida a la mayor sobra",
)
async def count_differences(
    count_id: PathId,
    principal: ReaderDep,
    counts: StockCountRepositoryDep,
    ingredients: IngredientRepositoryDep,
) -> CountDifferencesResponse:
    try:
        count, report = await ReadCountDifferences(counts)(principal.restaurant_id, count_id)
    except InventoryError as error:
        raise _http_error(error) from error
    by_id = await ingredients.get_many(
        principal.restaurant_id, [line.ingredient_id for line in report.lines]
    )
    return CountDifferencesResponse.build(count, report, by_id)


@router.get(
    "/ingredients/{ingredient_id}/count-differences",
    response_model=IngredientCountHistoryResponse,
    summary="Últimas diferencias de un insumo en conteos cerrados",
)
async def ingredient_count_history(
    ingredient_id: PathId,
    principal: ReaderDep,
    counts: StockCountRepositoryDep,
    ingredients: IngredientRepositoryDep,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_SIZE)] = DEFAULT_HISTORY_SIZE,
) -> IngredientCountHistoryResponse:
    try:
        ingredient, entries = await IngredientCountHistory(counts, ingredients)(
            principal.restaurant_id, ingredient_id, limit
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return IngredientCountHistoryResponse.build(ingredient, entries)


# -- Mermas por aprobar -----------------------------------------------------------------


@router.get(
    "/waste-approvals",
    response_model=WasteApprovalPageResponse,
    summary="Mermas que pidieron aprobación (filtra por estado)",
)
async def list_waste_approvals(
    principal: ApprovalsReaderDep,
    approvals: WasteApprovalRepositoryDep,
    ingredients: IngredientRepositoryDep,
    staff: StaffDirectoryDep,
    approval_status: Annotated[WasteApprovalStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> WasteApprovalPageResponse:
    page = await ListWasteApprovals(approvals)(
        WasteApprovalQuery(
            restaurant_id=principal.restaurant_id,
            status=approval_status,
            limit=limit,
            offset=offset,
        )
    )
    return WasteApprovalPageResponse(
        items=await _approval_responses(principal.restaurant_id, page.items, ingredients, staff),
        total=page.total,
    )


@router.post(
    "/waste-approvals/{approval_id}/approve",
    response_model=WasteApprovalResponse,
    summary="Aprobar una merma: recién ahí resta del stock",
)
async def approve_waste(
    approval_id: PathId,
    principal: ApproverDep,
    approvals: WasteApprovalRepositoryDep,
    ingredients: IngredientRepositoryDep,
    staff: StaffDirectoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> WasteApprovalResponse:
    approve = ApproveWaste(approvals, RegisterWaste(ingredients, ledger, activity), activity, clock)
    try:
        approval = await approve(principal.restaurant_id, principal.user_id, approval_id)
    except InventoryError as error:
        raise _http_error(error) from error
    responses = await _approval_responses(principal.restaurant_id, [approval], ingredients, staff)
    return responses[0]


@router.post(
    "/waste-approvals/{approval_id}/reject",
    response_model=WasteApprovalResponse,
    summary="Rechazar una merma con su motivo (no toca el stock)",
)
async def reject_waste(
    approval_id: PathId,
    payload: RejectWasteRequest,
    principal: ApproverDep,
    approvals: WasteApprovalRepositoryDep,
    ingredients: IngredientRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> WasteApprovalResponse:
    try:
        approval = await RejectWaste(ingredients, approvals, activity, clock)(
            principal.restaurant_id, principal.user_id, approval_id, payload.reason
        )
    except InventoryError as error:
        raise _http_error(error) from error
    responses = await _approval_responses(principal.restaurant_id, [approval], ingredients, staff)
    return responses[0]
