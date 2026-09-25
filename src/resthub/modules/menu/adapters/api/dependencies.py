"""Cableado del adaptador HTTP del menú."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.auth import SessionDep
from resthub.modules.menu.adapters.persistence.sqlalchemy_menu_repository import (
    SqlAlchemyMenuRepository,
)
from resthub.modules.menu.ports.menu_repository import MenuRepository


def get_menu_repository(session: SessionDep) -> MenuRepository:
    return SqlAlchemyMenuRepository(session)


MenuRepositoryDep = Annotated[MenuRepository, Depends(get_menu_repository)]
