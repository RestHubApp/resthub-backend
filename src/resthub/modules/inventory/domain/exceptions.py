"""Errores de dominio del inventario.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations


class InventoryError(Exception):
    """Raíz de los errores del módulo de inventario."""


class InvalidIngredient(InventoryError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidMovement(InventoryError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidRecipe(InventoryError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IngredientNotFound(InventoryError):
    """También cuando el insumo es de otro restaurante: no se delata que existe."""

    def __init__(self, ingredient_id: int) -> None:
        super().__init__(f"No existe el insumo {ingredient_id}.")
        self.ingredient_id = ingredient_id


class IngredientNameTaken(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Ya existe un insumo llamado {name!r}.")
        self.name = name


class IngredientInactive(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"El insumo {name} está desactivado.")
        self.name = name


class DishNotFound(InventoryError):
    def __init__(self, menu_item_id: int) -> None:
        super().__init__(f"No existe el plato {menu_item_id}.")
        self.menu_item_id = menu_item_id


class InvalidSupplier(InventoryError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SupplierNotFound(InventoryError):
    """También cuando el proveedor es de otro restaurante."""

    def __init__(self, supplier_id: int) -> None:
        super().__init__(f"No existe el proveedor {supplier_id}.")
        self.supplier_id = supplier_id


class SupplierNameTaken(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Ya existe un proveedor llamado {name!r}.")
        self.name = name


class InvalidPurchaseOrder(InventoryError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PurchaseOrderNotFound(InventoryError):
    """También cuando la orden es de otro restaurante."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"No existe la orden de compra {order_id}.")
        self.order_id = order_id
