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


class InvalidPackagingError(InventoryError):
    code = ErrorCode.INVALID_PACKAGING

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidQuantityEntryError(InventoryError):
    """Una cantidad en envases o por peso que no se puede pasar a la unidad del insumo."""

    code = ErrorCode.INVALID_QUANTITY_ENTRY

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidStockCountError(InventoryError):
    code = ErrorCode.INVALID_STOCK_COUNT

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StockCountNotFoundError(InventoryError):
    """También cuando el conteo es de otro restaurante."""

    code = ErrorCode.STOCK_COUNT_NOT_FOUND

    def __init__(self, count_id: int) -> None:
        super().__init__(f"No existe el conteo {count_id}.")
        self.count_id = count_id


class StockCountAlreadyOpenError(InventoryError):
    code = ErrorCode.STOCK_COUNT_ALREADY_OPEN

    def __init__(self, number: int | None = None) -> None:
        which = f" (#{number})" if number is not None else ""
        super().__init__(
            f"Ya hay un conteo físico en curso{which}: ciérralo o cancélalo antes de abrir otro."
        )
        self.number = number


class StockCountNotOpenError(InventoryError):
    code = ErrorCode.STOCK_COUNT_NOT_OPEN

    def __init__(self, number: int, status_label: str) -> None:
        super().__init__(f"El conteo #{number} está «{status_label}»: ya no se modifica.")
        self.number = number


class StockCountNotClosedError(InventoryError):
    code = ErrorCode.STOCK_COUNT_NOT_CLOSED

    def __init__(self, number: int) -> None:
        super().__init__(f"El conteo #{number} todavía no está cerrado: no hay diferencias.")
        self.number = number


class InvalidWasteApprovalError(InventoryError):
    code = ErrorCode.INVALID_WASTE_APPROVAL

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WasteApprovalNotFoundError(InventoryError):
    """También cuando la merma es de otro restaurante."""

    code = ErrorCode.WASTE_APPROVAL_NOT_FOUND

    def __init__(self, approval_id: int) -> None:
        super().__init__(f"No existe la merma por aprobar {approval_id}.")
        self.approval_id = approval_id


class WasteAlreadyDecidedError(InventoryError):
    code = ErrorCode.WASTE_ALREADY_DECIDED

    def __init__(self, status_label: str) -> None:
        super().__init__(f"Esa merma ya está «{status_label}».")
        self.status_label = status_label
