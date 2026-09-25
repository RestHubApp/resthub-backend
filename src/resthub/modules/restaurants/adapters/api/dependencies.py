"""Cableado del adaptador HTTP de restaurantes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.auth import SessionDep
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.ports.restaurant_repository import RestaurantRepository


def get_restaurant_repository(session: SessionDep) -> RestaurantRepository:
    return SqlAlchemyRestaurantRepository(session)


RestaurantRepositoryDep = Annotated[RestaurantRepository, Depends(get_restaurant_repository)]
