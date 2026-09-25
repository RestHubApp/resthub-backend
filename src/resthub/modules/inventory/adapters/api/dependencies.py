"""Cableado del adaptador HTTP del inventario."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.auth import SessionDep
from resthub.modules.inventory.adapters.persistence.directories import SqlDishDirectory
from resthub.modules.inventory.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyIngredientRepository,
    SqlAlchemyRecipeRepository,
    SqlAlchemyStockLedger,
)
from resthub.modules.inventory.ports.dish_directory import DishDirectory
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.recipe_repository import RecipeRepository
from resthub.modules.inventory.ports.stock_ledger import StockLedger


def get_ingredient_repository(session: SessionDep) -> IngredientRepository:
    return SqlAlchemyIngredientRepository(session)


def get_stock_ledger(session: SessionDep) -> StockLedger:
    return SqlAlchemyStockLedger(session)


def get_recipe_repository(session: SessionDep) -> RecipeRepository:
    return SqlAlchemyRecipeRepository(session)


def get_dish_directory(session: SessionDep) -> DishDirectory:
    return SqlDishDirectory(session)


IngredientRepositoryDep = Annotated[IngredientRepository, Depends(get_ingredient_repository)]
StockLedgerDep = Annotated[StockLedger, Depends(get_stock_ledger)]
RecipeRepositoryDep = Annotated[RecipeRepository, Depends(get_recipe_repository)]
DishDirectoryDep = Annotated[DishDirectory, Depends(get_dish_directory)]
