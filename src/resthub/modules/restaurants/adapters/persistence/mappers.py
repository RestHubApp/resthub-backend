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
        is_active=row.is_active,
        created_at=as_utc(row.created_at),
    )


def entity_to_row(restaurant: Restaurant) -> RestaurantRow:
    return RestaurantRow(
        name=restaurant.name,
        slug=restaurant.slug,
        timezone=restaurant.timezone,
        is_active=restaurant.is_active,
        created_at=restaurant.created_at,
    )
