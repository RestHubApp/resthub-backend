"""Restaurantes vistos desde la administración del sistema.

Todo exige una credencial de plataforma. El restaurante viene en la URL a
propósito: la plataforma no pertenece a ninguno y los administra a todos.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE
from resthub.modules.platform.adapters.api.dependencies import (
    PlatformActivityLogDep,
    RestaurantCatalogDep,
    RestaurantProvisioningDep,
    RestaurantsManagerDep,
    RestaurantsReaderDep,
)
from resthub.modules.platform.adapters.api.errors import to_http
from resthub.modules.platform.adapters.api.schemas import (
    CreateRestaurantRequest,
    NewOwnerRequest,
    OwnerResponse,
    PlatformUpdateRestaurantRequest,
    ResetOwnerPasswordRequest,
    RestaurantDetailResponse,
    RestaurantPageResponse,
    RestaurantSummaryResponse,
)
from resthub.modules.platform.domain.exceptions import PlatformError
from resthub.modules.platform.ports.restaurants import NewOwner, RestaurantChanges
from resthub.modules.platform.use_cases.manage_restaurants import (
    AddOwner,
    AddOwnerCommand,
    CreateRestaurant,
    CreateRestaurantCommand,
    ListRestaurants,
    ListRestaurantsQuery,
    ReadRestaurant,
    ResetOwnerPassword,
    ResetOwnerPasswordCommand,
    UpdateRestaurant,
    UpdateRestaurantCommand,
)

# Tope de las columnas enteras de PostgreSQL: más allá, la base responde con
# un error de datos (500) en vez de «no existe».
MAX_ID = 2**31 - 1

RestaurantIdPath = Annotated[int, Path(ge=1, le=MAX_ID)]
OwnerIdPath = Annotated[int, Path(ge=1, le=MAX_ID)]

router = APIRouter()


def _new_owner(payload: NewOwnerRequest) -> NewOwner:
    return NewOwner(
        full_name=payload.full_name, email=str(payload.email), password=payload.password
    )


@router.get("", response_model=RestaurantPageResponse, summary="Listar los restaurantes")
async def list_restaurants(
    _: RestaurantsReaderDep,
    catalog: RestaurantCatalogDep,
    search: Annotated[
        str | None, Query(max_length=120, description="Busca en nombre e identificador")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> RestaurantPageResponse:
    page = await ListRestaurants(catalog)(
        ListRestaurantsQuery(search=search, limit=limit, offset=offset)
    )
    return RestaurantPageResponse(
        items=[RestaurantSummaryResponse.from_summary(item) for item in page.items],
        total=page.total,
    )


@router.post(
    "",
    response_model=RestaurantDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta un restaurante y su primer encargado",
)
async def create_restaurant(
    payload: CreateRestaurantRequest,
    admin: RestaurantsManagerDep,
    provisioning: RestaurantProvisioningDep,
    catalog: RestaurantCatalogDep,
    activity: PlatformActivityLogDep,
) -> RestaurantDetailResponse:
    try:
        detail = await CreateRestaurant(provisioning, catalog, activity)(
            CreateRestaurantCommand(
                admin_id=admin.id or 0,
                name=payload.name,
                slug=payload.slug,
                timezone=payload.timezone,
                owner=_new_owner(payload.owner),
            )
        )
    except PlatformError as error:
        raise to_http(error) from error
    return RestaurantDetailResponse.from_detail(detail)


@router.get(
    "/{restaurant_id}", response_model=RestaurantDetailResponse, summary="Ficha de un restaurante"
)
async def read_restaurant(
    restaurant_id: RestaurantIdPath, _: RestaurantsReaderDep, catalog: RestaurantCatalogDep
) -> RestaurantDetailResponse:
    try:
        detail = await ReadRestaurant(catalog)(restaurant_id)
    except PlatformError as error:
        raise to_http(error) from error
    return RestaurantDetailResponse.from_detail(detail)


@router.patch(
    "/{restaurant_id}",
    response_model=RestaurantDetailResponse,
    summary="Editar, activar o desactivar un restaurante",
)
async def update_restaurant(
    restaurant_id: RestaurantIdPath,
    payload: PlatformUpdateRestaurantRequest,
    admin: RestaurantsManagerDep,
    provisioning: RestaurantProvisioningDep,
    catalog: RestaurantCatalogDep,
    activity: PlatformActivityLogDep,
) -> RestaurantDetailResponse:
    try:
        detail = await UpdateRestaurant(provisioning, catalog, activity)(
            UpdateRestaurantCommand(
                admin_id=admin.id or 0,
                restaurant_id=restaurant_id,
                changes=RestaurantChanges(
                    name=payload.name, timezone=payload.timezone, is_active=payload.is_active
                ),
            )
        )
    except PlatformError as error:
        raise to_http(error) from error
    return RestaurantDetailResponse.from_detail(detail)


@router.post(
    "/{restaurant_id}/owners",
    response_model=OwnerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agregar un encargado a un restaurante",
)
async def add_owner(
    restaurant_id: RestaurantIdPath,
    payload: NewOwnerRequest,
    admin: RestaurantsManagerDep,
    provisioning: RestaurantProvisioningDep,
    catalog: RestaurantCatalogDep,
    activity: PlatformActivityLogDep,
) -> OwnerResponse:
    try:
        owner = await AddOwner(provisioning, catalog, activity)(
            AddOwnerCommand(
                admin_id=admin.id or 0, restaurant_id=restaurant_id, owner=_new_owner(payload)
            )
        )
    except PlatformError as error:
        raise to_http(error) from error
    return OwnerResponse.from_account(owner)


@router.post(
    "/{restaurant_id}/owners/{owner_id}/password",
    response_model=OwnerResponse,
    summary="Restablecer la contraseña de un encargado (temporal)",
)
async def reset_owner_password(
    restaurant_id: RestaurantIdPath,
    owner_id: OwnerIdPath,
    payload: ResetOwnerPasswordRequest,
    admin: RestaurantsManagerDep,
    provisioning: RestaurantProvisioningDep,
    catalog: RestaurantCatalogDep,
    activity: PlatformActivityLogDep,
) -> OwnerResponse:
    """Para el encargado que olvidó su contraseña y no tiene a quién pedírsela.

    La contraseña es temporal: al entrar, el encargado solo puede cambiarla. Sus
    sesiones abiertas se cierran. 404 si la cuenta no es un encargado del local.
    """
    try:
        owner = await ResetOwnerPassword(provisioning, catalog, activity)(
            ResetOwnerPasswordCommand(
                admin_id=admin.id or 0,
                restaurant_id=restaurant_id,
                owner_id=owner_id,
                new_password=payload.new_password,
            )
        )
    except PlatformError as error:
        raise to_http(error) from error
    return OwnerResponse.from_account(owner)
