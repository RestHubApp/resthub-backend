"""Cableado del adaptador HTTP del menú."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.database import SessionDep
from resthub.modules.menu.adapters.persistence.directories import SqlAlchemyStockAvailabilityReader
from resthub.modules.menu.adapters.persistence.sqlalchemy_menu_repository import (
    SqlAlchemyMenuRepository,
)
from resthub.modules.menu.ports.menu_repository import MenuRepository
from resthub.modules.menu.ports.stock_availability import StockAvailabilityReader


def get_menu_repository(session: SessionDep) -> MenuRepository:
    return SqlAlchemyMenuRepository(session)


def get_stock_availability(session: SessionDep) -> StockAvailabilityReader:
    return SqlAlchemyStockAvailabilityReader(session)


MenuRepositoryDep = Annotated[MenuRepository, Depends(get_menu_repository)]
StockAvailabilityDep = Annotated[StockAvailabilityReader, Depends(get_stock_availability)]
