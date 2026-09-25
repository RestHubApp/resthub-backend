"""Adaptador de entrada HTTP del inventario.

Leer exige `inventory.read`; registrar compras, mermas, ajustes, insumos y
recetas exige `inventory.manage`. Hoy los dos son solo del encargado. Un insumo
o un plato de otro restaurante responde 404.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.modules.inventory.adapters.api.dependencies import (
    DishDirectoryDep,
    IngredientRepositoryDep,
    RecipeRepositoryDep,
    StockLedgerDep,
)
from resthub.modules.inventory.adapters.api.schemas import (
    AdjustmentRequest,
    CreateIngredientRequest,
    DishCostResponse,
    IngredientResponse,
    MovementPageResponse,
    MovementResponse,
    PurchaseRequest,
    RecipeResponse,
    ReplaceRecipeRequest,
    StockChangeResponse,
    UpdateIngredientRequest,
    WasteRequest,
)
from resthub.modules.inventory.domain.entities import MovementKind, RecipeLine
from resthub.modules.inventory.domain.exceptions import (
    DishNotFound,
    IngredientInactive,
    IngredientNameTaken,
    IngredientNotFound,
    InventoryError,
)
from resthub.modules.inventory.use_cases.manage_ingredients import (
    CreateIngredient,
    CreateIngredientCommand,
    ListStock,
    ListStockQuery,
    ReadIngredient,
    UpdateIngredient,
    UpdateIngredientCommand,
)
from resthub.modules.inventory.use_cases.recipes import (
    ListDishCosts,
    ReadRecipe,
    ReplaceRecipe,
    ReplaceRecipeCommand,
)
from resthub.modules.inventory.use_cases.register_movements import (
    ListMovements,
    ListMovementsQuery,
    RegisterAdjustment,
    RegisterAdjustmentCommand,
    RegisterPurchase,
    RegisterPurchaseCommand,
    RegisterWaste,
    RegisterWasteCommand,
)

router = APIRouter()

InventoryReaderDep = Annotated[Principal, Depends(require_permission(Permission.INVENTORY_READ))]
InventoryManagerDep = Annotated[Principal, Depends(require_permission(Permission.INVENTORY_MANAGE))]


def _http_error(error: InventoryError) -> HTTPException:
    if isinstance(error, IngredientNotFound | DishNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, IngredientNameTaken | IngredientInactive):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


# -- Insumos ----------------------------------------------------------------


@router.get("/ingredients", response_model=list[IngredientResponse], summary="Insumos con su stock")
async def list_ingredients(
    principal: InventoryReaderDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    include_inactive: bool = False,
) -> list[IngredientResponse]:
    levels = await ListStock(ingredients, ledger)(
        ListStockQuery(restaurant_id=principal.restaurant_id, include_inactive=include_inactive)
    )
    return [IngredientResponse.from_level(level) for level in levels]


@router.post(
    "/ingredients",
    response_model=IngredientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta un insumo",
)
async def create_ingredient(
    payload: CreateIngredientRequest,
    principal: InventoryManagerDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
) -> IngredientResponse:
    try:
        level = await CreateIngredient(ingredients, ledger, activity)(
            CreateIngredientCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                name=payload.name,
                unit=payload.unit,
                min_stock=payload.min_stock,
                unit_cost=payload.unit_cost,
            )
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return IngredientResponse.from_level(level)


@router.get(
    "/ingredients/{ingredient_id}", response_model=IngredientResponse, summary="Ver un insumo"
)
async def read_ingredient(
    ingredient_id: int,
    principal: InventoryReaderDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
) -> IngredientResponse:
    try:
        level = await ReadIngredient(ingredients, ledger)(principal.restaurant_id, ingredient_id)
    except InventoryError as error:
        raise _http_error(error) from error
    return IngredientResponse.from_level(level)


@router.patch(
    "/ingredients/{ingredient_id}",
    response_model=IngredientResponse,
    summary="Editar nombre, mínimo, costo o estado de un insumo",
)
async def update_ingredient(
    ingredient_id: int,
    payload: UpdateIngredientRequest,
    principal: InventoryManagerDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
) -> IngredientResponse:
    try:
        level = await UpdateIngredient(ingredients, ledger, activity)(
            UpdateIngredientCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                ingredient_id=ingredient_id,
                name=payload.name,
                min_stock=payload.min_stock,
                unit_cost=payload.unit_cost,
                is_active=payload.is_active,
            )
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return IngredientResponse.from_level(level)


@router.get(
    "/alerts/low-stock",
    response_model=list[IngredientResponse],
    summary="Insumos bajo el mínimo o en negativo, lo más urgente primero",
)
async def low_stock(
    principal: InventoryReaderDep, ingredients: IngredientRepositoryDep, ledger: StockLedgerDep
) -> list[IngredientResponse]:
    levels = await ListStock(ingredients, ledger)(
        ListStockQuery(restaurant_id=principal.restaurant_id, low_only=True)
    )
    return [IngredientResponse.from_level(level) for level in levels]


# -- Movimientos ------------------------------------------------------------


@router.get("/movements", response_model=MovementPageResponse, summary="Libro de movimientos")
async def list_movements(
    principal: InventoryReaderDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    ingredient_id: Annotated[int | None, Query(ge=1)] = None,
    kind: Annotated[list[MovementKind] | None, Query(description="Filtra por tipo")] = None,
    order_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MovementPageResponse:
    page = await ListMovements(ingredients, ledger)(
        ListMovementsQuery(
            restaurant_id=principal.restaurant_id,
            ingredient_id=ingredient_id,
            kinds=frozenset(kind) if kind else None,
            order_id=order_id,
            limit=limit,
            offset=offset,
        )
    )
    return MovementPageResponse(
        items=[MovementResponse.from_entry(entry) for entry in page.items], total=page.total
    )


@router.post(
    "/purchases",
    response_model=StockChangeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar una compra (actualiza el costo por promedio ponderado)",
)
async def register_purchase(
    payload: PurchaseRequest,
    principal: InventoryManagerDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
) -> StockChangeResponse:
    try:
        change = await RegisterPurchase(ingredients, ledger, activity)(
            RegisterPurchaseCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                ingredient_id=payload.ingredient_id,
                quantity=payload.quantity,
                unit_cost=payload.unit_cost,
                reason=payload.reason,
            )
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return StockChangeResponse.from_change(change)


@router.post(
    "/waste",
    response_model=StockChangeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar una merma con su motivo",
)
async def register_waste(
    payload: WasteRequest,
    principal: InventoryManagerDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
) -> StockChangeResponse:
    try:
        change = await RegisterWaste(ingredients, ledger, activity)(
            RegisterWasteCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                ingredient_id=payload.ingredient_id,
                quantity=payload.quantity,
                reason=payload.reason,
            )
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return StockChangeResponse.from_change(change)


@router.post(
    "/adjustments",
    response_model=StockChangeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ajustar el stock por diferencia o por conteo físico",
)
async def register_adjustment(
    payload: AdjustmentRequest,
    principal: InventoryManagerDep,
    ingredients: IngredientRepositoryDep,
    ledger: StockLedgerDep,
    activity: ActivityRecorderDep,
) -> StockChangeResponse:
    try:
        change = await RegisterAdjustment(ingredients, ledger, activity)(
            RegisterAdjustmentCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                ingredient_id=payload.ingredient_id,
                reason=payload.reason,
                quantity=payload.quantity,
                counted_stock=payload.counted_stock,
            )
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return StockChangeResponse.from_change(change)


# -- Recetas ----------------------------------------------------------------


@router.get(
    "/recipes",
    response_model=list[DishCostResponse],
    summary="Costo de receta y margen de cada plato",
)
async def list_dish_costs(
    principal: InventoryReaderDep,
    recipes: RecipeRepositoryDep,
    ingredients: IngredientRepositoryDep,
    dishes: DishDirectoryDep,
) -> list[DishCostResponse]:
    costs = await ListDishCosts(recipes, ingredients, dishes)(principal.restaurant_id)
    return [DishCostResponse.from_cost(cost) for cost in costs]


@router.get("/recipes/{menu_item_id}", response_model=RecipeResponse, summary="Receta de un plato")
async def read_recipe(
    menu_item_id: int,
    principal: InventoryReaderDep,
    recipes: RecipeRepositoryDep,
    ingredients: IngredientRepositoryDep,
    dishes: DishDirectoryDep,
) -> RecipeResponse:
    try:
        view = await ReadRecipe(recipes, ingredients, dishes)(principal.restaurant_id, menu_item_id)
    except InventoryError as error:
        raise _http_error(error) from error
    return RecipeResponse.from_view(view)


@router.put(
    "/recipes/{menu_item_id}",
    response_model=RecipeResponse,
    summary="Guardar la receta completa de un plato (vacía la borra)",
)
async def replace_recipe(
    menu_item_id: int,
    payload: ReplaceRecipeRequest,
    principal: InventoryManagerDep,
    recipes: RecipeRepositoryDep,
    ingredients: IngredientRepositoryDep,
    dishes: DishDirectoryDep,
    activity: ActivityRecorderDep,
) -> RecipeResponse:
    try:
        view = await ReplaceRecipe(recipes, ingredients, dishes, activity)(
            ReplaceRecipeCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                menu_item_id=menu_item_id,
                lines=[
                    RecipeLine(ingredient_id=line.ingredient_id, quantity=line.quantity)
                    for line in payload.lines
                ],
            )
        )
    except InventoryError as error:
        raise _http_error(error) from error
    return RecipeResponse.from_view(view)
