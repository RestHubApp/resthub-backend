"""Errores de dominio del inventario.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations

from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class InventoryError(Exception):
    """Raíz de los errores del módulo de inventario."""

    code: ClassVar[ErrorCode]


class InvalidIngredientError(InventoryError):
    code = ErrorCode.INVALID_INGREDIENT

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidMovementError(InventoryError):
    code = ErrorCode.INVALID_MOVEMENT

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidRecipeError(InventoryError):
    code = ErrorCode.INVALID_RECIPE

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IngredientNotFoundError(InventoryError):
    """También cuando el insumo es de otro restaurante: no se delata que existe."""

    code = ErrorCode.INGREDIENT_NOT_FOUND

    def __init__(self, ingredient_id: int) -> None:
        super().__init__(f"No existe el insumo {ingredient_id}.")
        self.ingredient_id = ingredient_id


class IngredientNameTakenError(InventoryError):
    code = ErrorCode.INGREDIENT_NAME_TAKEN

    def __init__(self, name: str) -> None:
        super().__init__(f"Ya existe un insumo llamado {name!r}.")
        self.name = name


class IngredientInactiveError(InventoryError):
    code = ErrorCode.INGREDIENT_INACTIVE

    def __init__(self, name: str) -> None:
        super().__init__(f"El insumo {name} está desactivado.")
        self.name = name


class DishNotFoundError(InventoryError):
    code = ErrorCode.DISH_NOT_FOUND

    def __init__(self, menu_item_id: int) -> None:
        super().__init__(f"No existe el plato {menu_item_id}.")
        self.menu_item_id = menu_item_id


class InvalidSupplierError(InventoryError):
    code = ErrorCode.INVALID_SUPPLIER

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SupplierNotFoundError(InventoryError):
    """También cuando el proveedor es de otro restaurante."""

    code = ErrorCode.SUPPLIER_NOT_FOUND

    def __init__(self, supplier_id: int) -> None:
        super().__init__(f"No existe el proveedor {supplier_id}.")
        self.supplier_id = supplier_id


class SupplierNameTakenError(InventoryError):
    code = ErrorCode.SUPPLIER_NAME_TAKEN

    def __init__(self, name: str) -> None:
        super().__init__(f"Ya existe un proveedor llamado {name!r}.")
        self.name = name


class InvalidPurchaseOrderError(InventoryError):
    code = ErrorCode.INVALID_PURCHASE_ORDER

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PurchaseOrderNotFoundError(InventoryError):
    """También cuando la orden es de otro restaurante."""

    code = ErrorCode.PURCHASE_ORDER_NOT_FOUND

    def __init__(self, order_id: int) -> None:
        super().__init__(f"No existe la orden de compra {order_id}.")
        self.order_id = order_id
