"""Errores de dominio del menú.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations

from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class MenuError(Exception):
    """Raíz de los errores del módulo del menú."""

    code: ClassVar[ErrorCode]


class InvalidMenuCategoryError(MenuError):
    code = ErrorCode.INVALID_MENU_CATEGORY

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidMenuItemError(MenuError):
    code = ErrorCode.INVALID_MENU_ITEM

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CategoryNotFoundError(MenuError):
    """También cuando la categoría existe pero es de otro restaurante.

    Responder distinto le diría a un encargado qué identificadores usa el resto
    de los locales.
    """

    code = ErrorCode.CATEGORY_NOT_FOUND

    def __init__(self, category_id: int) -> None:
        super().__init__(f"No existe la categoría {category_id}.")
        self.category_id = category_id


class MenuItemNotFoundError(MenuError):
    """También cuando el plato es de otro restaurante, o inactivo para el mesero."""

    code = ErrorCode.MENU_ITEM_NOT_FOUND

    def __init__(self, item_id: int) -> None:
        super().__init__(f"No existe el plato {item_id}.")
        self.item_id = item_id


class CategoryNameTakenError(MenuError):
    code = ErrorCode.CATEGORY_NAME_TAKEN

    def __init__(self, name: str) -> None:
        super().__init__(f"Ya existe una categoría llamada {name!r}.")
        self.name = name


class MenuItemNameTakenError(MenuError):
    code = ErrorCode.MENU_ITEM_NAME_TAKEN

    def __init__(self, name: str) -> None:
        super().__init__(f"Ya existe un plato llamado {name!r}.")
        self.name = name


class CategoryNotEmptyError(MenuError):
    code = ErrorCode.CATEGORY_NOT_EMPTY

    def __init__(self, category_id: int) -> None:
        super().__init__(
            "La categoría todavía tiene platos. Muévelos a otra o desactívala en vez de borrarla."
        )
        self.category_id = category_id


class InvalidOrderingError(MenuError):
    code = ErrorCode.INVALID_ORDERING

    def __init__(self) -> None:
        super().__init__(
            "El nuevo orden tiene que nombrar exactamente una vez a cada elemento de la lista."
        )
