"""Traducción entre la fila de la tabla y la entidad de dominio."""

from __future__ import annotations

from resthub.core.timestamps import as_utc
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow
from resthub.modules.restaurants.domain.entities import Restaurant


def row_to_entity(row: RestaurantRow) -> Restaurant:
    return Restaurant(
        id=row.id,
        name=row.name,
        slug=row.slug,
        timezone=row.timezone,
        max_waiter_discount_percent=row.max_waiter_discount_percent,
        auto_out_of_stock=row.auto_out_of_stock,
        external_ai_enabled=row.external_ai_enabled,
        is_active=row.is_active,
        is_sandbox=row.is_sandbox,
        created_at=as_utc(row.created_at),
    )


def entity_to_row(restaurant: Restaurant) -> RestaurantRow:
    return RestaurantRow(
        name=restaurant.name,
        slug=restaurant.slug,
        timezone=restaurant.timezone,
        max_waiter_discount_percent=restaurant.max_waiter_discount_percent,
        auto_out_of_stock=restaurant.auto_out_of_stock,
        external_ai_enabled=restaurant.external_ai_enabled,
        is_active=restaurant.is_active,
        is_sandbox=restaurant.is_sandbox,
        created_at=restaurant.created_at,
    )
