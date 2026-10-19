"""Cableado del adaptador HTTP del inventario."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.database import SessionDep
from resthub.modules.inventory.adapters.persistence.directories import (
    SqlAlchemyDishDirectory,
    SqlAlchemyOrderDirectory,
    SqlAlchemyStaffDirectory,
    SqlAlchemyWasteApprovalPolicy,
)
from resthub.modules.inventory.adapters.persistence.sqlalchemy_counts import (
    SqlAlchemyStockCountRepository,
    SqlAlchemyWasteApprovalRepository,
)
from resthub.modules.inventory.adapters.persistence.sqlalchemy_purchasing import (
    SqlAlchemyPurchaseOrderRepository,
    SqlAlchemySupplierRepository,
    SqlAlchemyUsageReader,
)
from resthub.modules.inventory.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyIngredientRepository,
    SqlAlchemyRecipeRepository,
    SqlAlchemyStockLedgerRepository,
)
from resthub.modules.inventory.ports.dish_directory import DishDirectory
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.order_directory import OrderDirectory
from resthub.modules.inventory.ports.purchasing_repository import (
    PurchaseOrderRepository,
    SupplierRepository,
    UsageReader,
)
from resthub.modules.inventory.ports.recipe_repository import RecipeRepository
from resthub.modules.inventory.ports.staff_directory import StaffDirectory
from resthub.modules.inventory.ports.stock_count_repository import StockCountRepository
from resthub.modules.inventory.ports.stock_ledger import StockLedgerRepository
from resthub.modules.inventory.ports.waste_approval_repository import (
    WasteApprovalPolicy,
    WasteApprovalRepository,
)


def get_ingredient_repository(session: SessionDep) -> IngredientRepository:
    return SqlAlchemyIngredientRepository(session)


def get_stock_ledger(session: SessionDep) -> StockLedgerRepository:
    return SqlAlchemyStockLedgerRepository(session)


def get_recipe_repository(session: SessionDep) -> RecipeRepository:
    return SqlAlchemyRecipeRepository(session)


def get_dish_directory(session: SessionDep) -> DishDirectory:
    return SqlAlchemyDishDirectory(session)


def get_order_directory(session: SessionDep) -> OrderDirectory:
    return SqlAlchemyOrderDirectory(session)


IngredientRepositoryDep = Annotated[IngredientRepository, Depends(get_ingredient_repository)]
StockLedgerDep = Annotated[StockLedgerRepository, Depends(get_stock_ledger)]
RecipeRepositoryDep = Annotated[RecipeRepository, Depends(get_recipe_repository)]
DishDirectoryDep = Annotated[DishDirectory, Depends(get_dish_directory)]
OrderDirectoryDep = Annotated[OrderDirectory, Depends(get_order_directory)]


def get_supplier_repository(session: SessionDep) -> SupplierRepository:
    return SqlAlchemySupplierRepository(session)


def get_purchase_order_repository(session: SessionDep) -> PurchaseOrderRepository:
    return SqlAlchemyPurchaseOrderRepository(session)


def get_usage_reader(session: SessionDep) -> UsageReader:
    return SqlAlchemyUsageReader(session)


SupplierRepositoryDep = Annotated[SupplierRepository, Depends(get_supplier_repository)]
PurchaseOrderRepositoryDep = Annotated[
    PurchaseOrderRepository, Depends(get_purchase_order_repository)
]
UsageReaderDep = Annotated[UsageReader, Depends(get_usage_reader)]


def get_stock_count_repository(session: SessionDep) -> StockCountRepository:
    return SqlAlchemyStockCountRepository(session)


def get_waste_approval_repository(session: SessionDep) -> WasteApprovalRepository:
    return SqlAlchemyWasteApprovalRepository(session)


def get_waste_approval_policy(session: SessionDep) -> WasteApprovalPolicy:
    return SqlAlchemyWasteApprovalPolicy(session)


def get_staff_directory(session: SessionDep) -> StaffDirectory:
    return SqlAlchemyStaffDirectory(session)


StockCountRepositoryDep = Annotated[StockCountRepository, Depends(get_stock_count_repository)]
WasteApprovalRepositoryDep = Annotated[
    WasteApprovalRepository, Depends(get_waste_approval_repository)
]
WasteApprovalPolicyDep = Annotated[WasteApprovalPolicy, Depends(get_waste_approval_policy)]
StaffDirectoryDep = Annotated[StaffDirectory, Depends(get_staff_directory)]
