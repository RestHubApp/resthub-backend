"""Adaptador de entrada HTTP del restaurante propio.

Es un recurso singular (`/restaurant` y no `/restaurants/{id}`): cada cuenta
pertenece a un solo restaurante y no hay forma de nombrar a otro.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import PrincipalDep, require_permission
from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.modules.restaurants.adapters.api.dependencies import RestaurantRepositoryDep
from resthub.modules.restaurants.adapters.api.schemas import (
    RestaurantResponse,
    UpdateRestaurantRequest,
)
from resthub.modules.restaurants.domain.exceptions import (
    InvalidDiscountLimit,
    InvalidRestaurantName,
    InvalidTimezone,
    RestaurantNotFound,
)
from resthub.modules.restaurants.use_cases.read_restaurant import ReadOwnRestaurant
from resthub.modules.restaurants.use_cases.update_restaurant import (
    UpdateRestaurant,
    UpdateRestaurantCommand,
)

router = APIRouter()

RestaurantManagerDep = Annotated[
    Principal, Depends(require_permission(Permission.RESTAURANT_MANAGE))
]


@router.get("", response_model=RestaurantResponse, summary="Restaurante de la cuenta")
async def read_restaurant(
    principal: PrincipalDep, restaurants: RestaurantRepositoryDep
) -> RestaurantResponse:
    # Cualquier cuenta lee el suyo: el mesero también necesita, por ejemplo, la
    # zona horaria para mostrar las horas de los pedidos.
    try:
        restaurant = await ReadOwnRestaurant(restaurants)(principal.restaurant_id)
    except RestaurantNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    return RestaurantResponse.from_entity(restaurant)


@router.patch("", response_model=RestaurantResponse, summary="Editar el restaurante propio")
async def update_restaurant(
    payload: UpdateRestaurantRequest,
    principal: RestaurantManagerDep,
    restaurants: RestaurantRepositoryDep,
    activity: ActivityRecorderDep,
) -> RestaurantResponse:
    try:
        restaurant = await UpdateRestaurant(restaurants, activity)(
            UpdateRestaurantCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                name=payload.name,
                timezone=payload.timezone,
                max_waiter_discount_percent=payload.max_waiter_discount_percent,
                auto_out_of_stock=payload.auto_out_of_stock,
            )
        )
    except RestaurantNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except (InvalidRestaurantName, InvalidTimezone, InvalidDiscountLimit) as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    return RestaurantResponse.from_entity(restaurant)
