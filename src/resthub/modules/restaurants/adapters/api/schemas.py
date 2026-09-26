"""Contrato HTTP del módulo de restaurantes.

No hay ningún `restaurant_id` en los cuerpos de entrada: el restaurante sale
siempre de la credencial de quien pregunta.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from resthub.modules.restaurants.domain.entities import MAX_NAME_LENGTH, Restaurant


class RestaurantResponse(BaseModel):
    id: int
    name: str
    slug: str
    timezone: str
    # Lo que un mesero puede descontar al cobrar sin el encargado.
    max_waiter_discount_percent: Decimal
    # Agotar solos los platos cuya receta no alcanza con el stock.
    auto_out_of_stock: bool
    is_active: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, restaurant: Restaurant) -> RestaurantResponse:
        return cls(
            id=restaurant.id or 0,
            name=restaurant.name,
            slug=restaurant.slug,
            timezone=restaurant.timezone,
            max_waiter_discount_percent=restaurant.max_waiter_discount_percent,
            auto_out_of_stock=restaurant.auto_out_of_stock,
            is_active=restaurant.is_active,
            created_at=restaurant.created_at,
        )


class UpdateRestaurantRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    max_waiter_discount_percent: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=5, decimal_places=2
    )
    auto_out_of_stock: bool | None = None
