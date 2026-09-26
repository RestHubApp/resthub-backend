"""Adaptador de entrada HTTP del menú.

Leer exige `menu.read`, que de entrada tienen encargado y mesero; lo demás exige
`menu.manage`. Una categoría o un plato de otro restaurante responde 404, igual
que uno que no existe.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.core.realtime_broker import EventPublisherDep
from resthub.modules.menu.adapters.api.dependencies import (
    MenuRepositoryDep,
    StockAvailabilityDep,
)
from resthub.modules.menu.adapters.api.schemas import (
    CreateCategoryRequest,
    CreateMenuItemRequest,
    MenuCategoryResponse,
    MenuItemResponse,
    MenuResponse,
    MenuSectionResponse,
    ReorderRequest,
    SetAvailabilityRequest,
    UpdateCategoryRequest,
    UpdateMenuItemRequest,
)
from resthub.modules.menu.domain.exceptions import (
    CategoryNameTaken,
    CategoryNotEmpty,
    CategoryNotFound,
    InvalidMenuCategory,
    InvalidMenuItem,
    InvalidOrdering,
    MenuError,
    MenuItemNameTaken,
    MenuItemNotFound,
)
from resthub.modules.menu.use_cases.manage_categories import (
    CreateCategory,
    CreateCategoryCommand,
    DeleteCategory,
    DeleteCategoryCommand,
    ReorderCategories,
    ReorderCategoriesCommand,
    UpdateCategory,
    UpdateCategoryCommand,
)
from resthub.modules.menu.use_cases.manage_items import (
    CreateMenuItem,
    CreateMenuItemCommand,
    ReorderItems,
    ReorderItemsCommand,
    SetAvailability,
    SetAvailabilityCommand,
    UpdateMenuItem,
    UpdateMenuItemCommand,
)
from resthub.modules.menu.use_cases.read_menu import ReadMenu, ReadMenuItem, ReadMenuQuery

router = APIRouter()

MenuReaderDep = Annotated[Principal, Depends(require_permission(Permission.MENU_READ))]
MenuManagerDep = Annotated[Principal, Depends(require_permission(Permission.MENU_MANAGE))]


def _http_error(error: MenuError) -> HTTPException:
    """Un solo lugar para traducir los errores del dominio a HTTP."""
    if isinstance(error, CategoryNotFound | MenuItemNotFound):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, CategoryNameTaken | MenuItemNameTaken | CategoryNotEmpty):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, InvalidMenuCategory | InvalidMenuItem | InvalidOrdering):
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        code = status.HTTP_400_BAD_REQUEST
    return HTTPException(code, str(error))


def _can_manage(principal: Principal) -> bool:
    return Permission.MENU_MANAGE in principal.permissions


@router.get("", response_model=MenuResponse, summary="Menú completo agrupado por categoría")
async def read_menu(
    principal: MenuReaderDep,
    menu: MenuRepositoryDep,
    stock: StockAvailabilityDep,
    include_inactive: Annotated[
        bool, Query(description="Incluye lo desactivado; solo con menu.manage")
    ] = False,
) -> MenuResponse:
    # Sin `menu.manage` el parámetro se ignora en vez de rechazarse: el mesero
    # ve la carta vigente y lo que hoy no hay, nunca lo retirado.
    sections = await ReadMenu(menu, stock)(
        ReadMenuQuery(
            restaurant_id=principal.restaurant_id,
            include_inactive=include_inactive and _can_manage(principal),
        )
    )
    return MenuResponse(
        categories=[MenuSectionResponse.from_section(section) for section in sections]
    )


@router.post(
    "/categories",
    response_model=MenuCategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una categoría",
)
async def create_category(
    payload: CreateCategoryRequest,
    principal: MenuManagerDep,
    menu: MenuRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> MenuCategoryResponse:
    try:
        category = await CreateCategory(menu, activity, events)(
            CreateCategoryCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                name=payload.name,
            )
        )
    except MenuError as error:
        raise _http_error(error) from error
    return MenuCategoryResponse.from_entity(category)


@router.put(
    "/categories/order",
    response_model=list[MenuCategoryResponse],
    summary="Reordenar las categorías",
)
async def reorder_categories(
    payload: ReorderRequest,
    principal: MenuManagerDep,
    menu: MenuRepositoryDep,
    events: EventPublisherDep,
) -> list[MenuCategoryResponse]:
    try:
        categories = await ReorderCategories(menu, events)(
            ReorderCategoriesCommand(
                restaurant_id=principal.restaurant_id, category_ids=payload.ids
            )
        )
    except MenuError as error:
        raise _http_error(error) from error
    return [MenuCategoryResponse.from_entity(category) for category in categories]


@router.patch(
    "/categories/{category_id}",
    response_model=MenuCategoryResponse,
    summary="Renombrar, activar o desactivar una categoría",
)
async def update_category(
    category_id: int,
    payload: UpdateCategoryRequest,
    principal: MenuManagerDep,
    menu: MenuRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> MenuCategoryResponse:
    try:
        category = await UpdateCategory(menu, activity, events)(
            UpdateCategoryCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                category_id=category_id,
                name=payload.name,
                is_active=payload.is_active,
            )
        )
    except MenuError as error:
        raise _http_error(error) from error
    return MenuCategoryResponse.from_entity(category)


@router.delete(
    "/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar una categoría vacía",
)
async def delete_category(
    category_id: int,
    principal: MenuManagerDep,
    menu: MenuRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> Response:
    try:
        await DeleteCategory(menu, activity, events)(
            DeleteCategoryCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                category_id=category_id,
            )
        )
    except MenuError as error:
        raise _http_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/categories/{category_id}/items/order",
    response_model=list[MenuItemResponse],
    summary="Reordenar los platos de una categoría",
)
async def reorder_items(
    category_id: int,
    payload: ReorderRequest,
    principal: MenuManagerDep,
    menu: MenuRepositoryDep,
    events: EventPublisherDep,
) -> list[MenuItemResponse]:
    try:
        items = await ReorderItems(menu, events)(
            ReorderItemsCommand(
                restaurant_id=principal.restaurant_id,
                category_id=category_id,
                item_ids=payload.ids,
            )
        )
    except MenuError as error:
        raise _http_error(error) from error
    return [MenuItemResponse.from_entity(item) for item in items]


@router.post(
    "/items",
    response_model=MenuItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agregar un plato",
)
async def create_item(
    payload: CreateMenuItemRequest,
    principal: MenuManagerDep,
    menu: MenuRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> MenuItemResponse:
    try:
        item = await CreateMenuItem(menu, activity, events)(
            CreateMenuItemCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                category_id=payload.category_id,
                name=payload.name,
                description=payload.description,
                price=payload.price,
                is_available=payload.is_available,
                modifier_groups=tuple(group.to_entity() for group in payload.modifier_groups),
            )
        )
    except MenuError as error:
        raise _http_error(error) from error
    return MenuItemResponse.from_entity(item)


@router.get("/items/{item_id}", response_model=MenuItemResponse, summary="Ver un plato")
async def read_item(
    item_id: int, principal: MenuReaderDep, menu: MenuRepositoryDep
) -> MenuItemResponse:
    try:
        item = await ReadMenuItem(menu)(
            principal.restaurant_id, item_id, include_inactive=_can_manage(principal)
        )
    except MenuError as error:
        raise _http_error(error) from error
    return MenuItemResponse.from_entity(item)


@router.patch("/items/{item_id}", response_model=MenuItemResponse, summary="Editar un plato")
async def update_item(
    item_id: int,
    payload: UpdateMenuItemRequest,
    principal: MenuManagerDep,
    menu: MenuRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> MenuItemResponse:
    try:
        item = await UpdateMenuItem(menu, activity, events)(
            UpdateMenuItemCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                item_id=item_id,
                category_id=payload.category_id,
                name=payload.name,
                description=payload.description,
                price=payload.price,
                is_active=payload.is_active,
                is_available=payload.is_available,
                modifier_groups=(
                    None
                    if payload.modifier_groups is None
                    else tuple(group.to_entity() for group in payload.modifier_groups)
                ),
            )
        )
    except MenuError as error:
        raise _http_error(error) from error
    return MenuItemResponse.from_entity(item)


@router.patch(
    "/items/{item_id}/availability",
    response_model=MenuItemResponse,
    summary='Marcar un plato como "hay" o "no hay" hoy',
)
async def set_availability(
    item_id: int,
    payload: SetAvailabilityRequest,
    principal: MenuManagerDep,
    menu: MenuRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> MenuItemResponse:
    try:
        item = await SetAvailability(menu, activity, events)(
            SetAvailabilityCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                item_id=item_id,
                is_available=payload.is_available,
            )
        )
    except MenuError as error:
        raise _http_error(error) from error
    return MenuItemResponse.from_entity(item)
