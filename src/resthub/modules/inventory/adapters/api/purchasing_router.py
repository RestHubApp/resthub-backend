"""Adaptador de entrada HTTP de proveedores y órdenes de compra.

Leer exige `inventory.read`; lo demás, `inventory.manage`. Cuelga del mismo
prefijo `/inventory` que el resto del inventario.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from resthub.core.activity import ActivityRecorder
from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.modules.inventory.adapters.api.dependencies import (
    IngredientRepositoryDep,
    PurchaseOrderRepositoryDep,
    StockLedgerDep,
    SupplierRepositoryDep,
    UsageReaderDep,
)
from resthub.modules.inventory.adapters.api.purchasing_schemas import (
    CreatePurchaseOrderRequest,
    EditPurchaseOrderRequest,
    PurchaseLineRequest,
    PurchaseOrderPageResponse,
    PurchaseOrderResponse,
    PurchaseSuggestionResponse,
    ReceivePurchaseOrderRequest,
    SupplierRequest,
    SupplierResponse,
)
from resthub.modules.inventory.domain.exceptions import (
    IngredientInactive,
    IngredientNotFound,
    InventoryError,
    PurchaseOrderNotFound,
    SupplierNameTaken,
    SupplierNotFound,
)
from resthub.modules.inventory.domain.purchasing import (
    PurchaseOrder,
    PurchaseOrderStatus,
    Receipt,
)
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.purchasing_repository import (
    PurchaseOrderQuery,
    PurchaseOrderRepository,
    SupplierRepository,
)
from resthub.modules.inventory.use_cases.purchasing import (
    ChangePurchaseOrderStatus,
    CreatePurchaseOrder,
    EditPurchaseOrder,
    LineData,
    ListPurchaseOrders,
    ReceivePurchaseOrder,
    SaveSupplier,
    SuggestPurchases,
    SupplierData,
    find_purchase_order,
)
from resthub.modules.inventory.use_cases.register_movements import RegisterPurchase

router = APIRouter()

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.INVENTORY_READ))]
ManagerDep = Annotated[Principal, Depends(require_permission(Permission.INVENTORY_MANAGE))]


def _http_error(error: InventoryError) -> HTTPException:
    if isinstance(error, SupplierNotFound | PurchaseOrderNotFound | IngredientNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, SupplierNameTaken | IngredientInactive):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


def _lines(lines: list[PurchaseLineRequest]) -> list[LineData]:
    return [
        LineData(ingredient_id=line.ingredient_id, quantity=line.quantity, unit_cost=line.unit_cost)
        for line in lines
    ]


async def _respond(
    restaurant_id: int,
    orders: list[PurchaseOrder],
    suppliers: SupplierRepository,
    ingredients: IngredientRepository,
) -> list[PurchaseOrderResponse]:
    por_proveedor = {s.id or 0: s for s in await suppliers.list_all(restaurant_id)}
    por_insumo = {i.id or 0: i for i in await ingredients.list_all(restaurant_id)}
    return [PurchaseOrderResponse.build(order, por_proveedor, por_insumo) for order in orders]


# -- Proveedores ---------------------------------------------------------------


@router.get("/suppliers", response_model=list[SupplierResponse], summary="Proveedores")
async def list_suppliers(
    principal: ReaderDep, suppliers: SupplierRepositoryDep
) -> list[SupplierResponse]:
    return [
        SupplierResponse.from_entity(s) for s in await suppliers.list_all(principal.restaurant_id)
    ]


@router.post(
    "/suppliers",
    response_model=SupplierResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta un proveedor",
)
async def create_supplier(
    payload: SupplierRequest,
    principal: ManagerDep,
    suppliers: SupplierRepositoryDep,
    activity: ActivityRecorderDep,
) -> SupplierResponse:
    try:
        supplier = await SaveSupplier(suppliers, activity)(
            principal.restaurant_id, principal.user_id, SupplierData(**payload.model_dump())
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return SupplierResponse.from_entity(supplier)


@router.put("/suppliers/{supplier_id}", response_model=SupplierResponse, summary="Editar")
async def update_supplier(
    supplier_id: int,
    payload: SupplierRequest,
    principal: ManagerDep,
    suppliers: SupplierRepositoryDep,
    activity: ActivityRecorderDep,
) -> SupplierResponse:
    try:
        supplier = await SaveSupplier(suppliers, activity)(
            principal.restaurant_id,
            principal.user_id,
            SupplierData(**payload.model_dump()),
            supplier_id,
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return SupplierResponse.from_entity(supplier)


# -- Órdenes de compra ---------------------------------------------------------


@router.get(
    "/purchase-suggestions",
    response_model=list[PurchaseSuggestionResponse],
    summary="Qué insumos pedir y cuánto",
)
async def purchase_suggestions(
    principal: ReaderDep, usage: UsageReaderDep, ingredients: IngredientRepositoryDep
) -> list[PurchaseSuggestionResponse]:
    suggestions = await SuggestPurchases(usage)(principal.restaurant_id)
    por_insumo = {i.id or 0: i for i in await ingredients.list_all(principal.restaurant_id)}
    return [
        PurchaseSuggestionResponse.build(s, por_insumo[s.ingredient_id])
        for s in suggestions
        if s.ingredient_id in por_insumo
    ]


@router.get(
    "/purchase-orders", response_model=PurchaseOrderPageResponse, summary="Órdenes de compra"
)
async def list_purchase_orders(
    principal: ReaderDep,
    orders: PurchaseOrderRepositoryDep,
    suppliers: SupplierRepositoryDep,
    ingredients: IngredientRepositoryDep,
    order_status: Annotated[PurchaseOrderStatus | None, Query(alias="status")] = None,
    supplier_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PurchaseOrderPageResponse:
    page = await ListPurchaseOrders(orders)(
        PurchaseOrderQuery(
            restaurant_id=principal.restaurant_id,
            status=order_status,
            supplier_id=supplier_id,
            limit=limit,
            offset=offset,
        )
    )
    return PurchaseOrderPageResponse(
        items=await _respond(principal.restaurant_id, page.items, suppliers, ingredients),
        total=page.total,
    )


@router.post(
    "/purchase-orders",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una orden de compra (borrador)",
)
async def create_purchase_order(
    payload: CreatePurchaseOrderRequest,
    principal: ManagerDep,
    orders: PurchaseOrderRepositoryDep,
    suppliers: SupplierRepositoryDep,
    ingredients: IngredientRepositoryDep,
    activity: ActivityRecorderDep,
) -> PurchaseOrderResponse:
    try:
        order = await CreatePurchaseOrder(orders, suppliers, ingredients, activity)(
            principal.restaurant_id,
            principal.user_id,
            payload.supplier_id,
            _lines(payload.lines),
            payload.notes,
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return (await _respond(principal.restaurant_id, [order], suppliers, ingredients))[0]


@router.get(
    "/purchase-orders/{order_id}", response_model=PurchaseOrderResponse, summary="Una orden"
)
async def read_purchase_order(
    order_id: int,
    principal: ReaderDep,
    orders: PurchaseOrderRepositoryDep,
    suppliers: SupplierRepositoryDep,
    ingredients: IngredientRepositoryDep,
) -> PurchaseOrderResponse:
    try:
        order = await find_purchase_order(orders, principal.restaurant_id, order_id)
    except InventoryError as error:
        raise _http_error(error) from error
    return (await _respond(principal.restaurant_id, [order], suppliers, ingredients))[0]


@router.put(
    "/purchase-orders/{order_id}", response_model=PurchaseOrderResponse, summary="Editar borrador"
)
async def edit_purchase_order(
    order_id: int,
    payload: EditPurchaseOrderRequest,
    principal: ManagerDep,
    orders: PurchaseOrderRepositoryDep,
    suppliers: SupplierRepositoryDep,
    ingredients: IngredientRepositoryDep,
) -> PurchaseOrderResponse:
    try:
        order = await EditPurchaseOrder(orders, ingredients)(
            principal.restaurant_id, order_id, _lines(payload.lines), payload.notes
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return (await _respond(principal.restaurant_id, [order], suppliers, ingredients))[0]


async def _change(
    action: str,
    order_id: int,
    principal: Principal,
    context: tuple[
        PurchaseOrderRepository, SupplierRepository, IngredientRepository, ActivityRecorder
    ],
) -> PurchaseOrderResponse:
    orders, suppliers, ingredients, activity = context
    try:
        order = await ChangePurchaseOrderStatus(orders, activity)(
            principal.restaurant_id, principal.user_id, order_id, action
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return (await _respond(principal.restaurant_id, [order], suppliers, ingredients))[0]


@router.post(
    "/purchase-orders/{order_id}/send",
    response_model=PurchaseOrderResponse,
    summary="Marcar enviada al proveedor",
)
async def send_purchase_order(
    order_id: int,
    principal: ManagerDep,
    orders: PurchaseOrderRepositoryDep,
    suppliers: SupplierRepositoryDep,
    ingredients: IngredientRepositoryDep,
    activity: ActivityRecorderDep,
) -> PurchaseOrderResponse:
    return await _change("send", order_id, principal, (orders, suppliers, ingredients, activity))


@router.post(
    "/purchase-orders/{order_id}/cancel",
    response_model=PurchaseOrderResponse,
    summary="Cancelar la orden",
)
async def cancel_purchase_order(
    order_id: int,
    principal: ManagerDep,
    orders: PurchaseOrderRepositoryDep,
    suppliers: SupplierRepositoryDep,
    ingredients: IngredientRepositoryDep,
    activity: ActivityRecorderDep,
) -> PurchaseOrderResponse:
    return await _change("cancel", order_id, principal, (orders, suppliers, ingredients, activity))


@router.post(
    "/purchase-orders/{order_id}/receive",
    response_model=PurchaseOrderResponse,
    summary="Recibir la mercadería (entra al stock como compras)",
)
async def receive_purchase_order(
    order_id: int,
    payload: ReceivePurchaseOrderRequest,
    principal: ManagerDep,
    orders: PurchaseOrderRepositoryDep,
    suppliers: SupplierRepositoryDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
) -> PurchaseOrderResponse:
    receive = ReceivePurchaseOrder(
        orders, suppliers, RegisterPurchase(ingredients, ledger, activity), activity
    )
    try:
        order = await receive(
            principal.restaurant_id,
            principal.user_id,
            order_id,
            [
                Receipt(line_id=line.line_id, quantity=line.quantity, unit_cost=line.unit_cost)
                for line in payload.lines
            ],
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return (await _respond(principal.restaurant_id, [order], suppliers, ingredients))[0]
