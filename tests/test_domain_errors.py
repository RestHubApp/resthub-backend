"""Errores de dominio de cuentas e inventario: el mensaje que ve la persona y sus datos.

El API responde con el texto del error (`detail`), así que ese texto es parte
del comportamiento: decirle a un encargado «No puedes cambiar tu propio rol» o
cuál es el insumo desactivado es lo que le permite corregir la operación.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from resthub.core.permissions import Permission
from resthub.modules.accounts.domain import exceptions as accounts
from resthub.modules.inventory.domain import exceptions as inventory


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: accounts.InvalidEmailError("ana@"), "El correo electrónico no es válido: 'ana@'"),
        (lambda: accounts.InvalidFullNameError("Falta el nombre."), "Falta el nombre."),
        (lambda: accounts.WeakPasswordError("Muy corta."), "Muy corta."),
        (
            lambda: accounts.EmailAlreadyRegisteredError("ana@rosa.pe"),
            "Ya existe una cuenta con el correo 'ana@rosa.pe'.",
        ),
        (lambda: accounts.UserNotFoundError(4), "No existe la cuenta 4."),
        (lambda: accounts.InvalidCredentialsError(), "El correo o la contraseña no son correctos."),
        (
            lambda: accounts.InactiveRestaurantError(),
            "El restaurante de esta cuenta está desactivado.",
        ),
        (lambda: accounts.WrongCurrentPasswordError(), "La contraseña actual no es correcta."),
        (lambda: accounts.CannotDeactivateSelfError(), "No puedes desactivar tu propia cuenta."),
        (lambda: accounts.CannotChangeOwnRoleError(), "No puedes cambiar tu propio rol."),
        (
            lambda: accounts.CannotResetOwnPasswordError(),
            "Para tu propia cuenta usa el cambio de contraseña, que pide la actual.",
        ),
        (
            lambda: accounts.RestaurantAlreadyHasStaffError(3),
            "El restaurante 3 ya tiene personal registrado.",
        ),
        (
            lambda: accounts.CannotManageStrongerAccountError(),
            "No puedes gestionar una cuenta que tiene permisos que tú no tienes.",
        ),
        (lambda: accounts.RoleNotFoundError(8), "No existe el rol 8."),
        (lambda: accounts.InvalidRoleNameError("Nombre vacío."), "Nombre vacío."),
        (lambda: accounts.RoleNameTakenError("Cocinero"), "Ya existe un rol llamado 'Cocinero'."),
        (
            lambda: accounts.RoleNotEditableError(),
            "El rol de encargado no se edita: siempre tiene todos los permisos.",
        ),
        (
            lambda: accounts.BaseRoleNameFixedError("Mesero"),
            "El rol Mesero no cambia de nombre; solo sus permisos.",
        ),
        (
            lambda: accounts.BaseRoleNotDeletableError("Mesero"),
            "El rol Mesero es de todo restaurante y no se elimina.",
        ),
        (
            lambda: accounts.RoleInUseError("Cocinero"),
            "El rol Cocinero tiene personal asignado; cámbialo de rol antes.",
        ),
        (
            lambda: accounts.CannotGrantPermissionsError(frozenset({Permission.CASH_MANAGE.value})),
            "No puedes dar permisos que no tienes.",
        ),
        (
            lambda: accounts.CannotManageStrongerRoleError(),
            "No puedes cambiar un rol que tiene permisos que tú no tienes.",
        ),
        (
            lambda: accounts.InvalidPreviewCodeError(),
            "El código de vista previa no es válido o ya venció.",
        ),
        (lambda: accounts.NotASandboxAccountError(5), "La cuenta 5 no es del local de muestra."),
        (
            lambda: accounts.PreviewSessionRestrictedError(),
            "En la vista previa no se cambia la contraseña.",
        ),
        (lambda: inventory.InvalidIngredientError("Sin nombre."), "Sin nombre."),
        (lambda: inventory.InvalidMovementError("Resta."), "Resta."),
        (lambda: inventory.InvalidRecipeError("Repetido."), "Repetido."),
        (lambda: inventory.IngredientNotFoundError(2), "No existe el insumo 2."),
        (
            lambda: inventory.IngredientNameTakenError("Arroz"),
            "Ya existe un insumo llamado 'Arroz'.",
        ),
        (lambda: inventory.IngredientInactiveError("Arroz"), "El insumo Arroz está desactivado."),
        (lambda: inventory.DishNotFoundError(9), "No existe el plato 9."),
        (lambda: inventory.InvalidSupplierError("Sin nombre."), "Sin nombre."),
        (lambda: inventory.SupplierNotFoundError(6), "No existe el proveedor 6."),
        (
            lambda: inventory.SupplierNameTakenError("Makro"),
            "Ya existe un proveedor llamado 'Makro'.",
        ),
        (lambda: inventory.InvalidPurchaseOrderError("Vacía."), "Vacía."),
        (lambda: inventory.PurchaseOrderNotFoundError(7), "No existe la orden de compra 7."),
    ],
)
def test_each_error_explains_what_happened(factory: Callable[[], Exception], message: str) -> None:
    assert str(factory()) == message


def test_account_errors_keep_their_data() -> None:
    assert accounts.InvalidEmailError("ana@").value == "ana@"
    assert accounts.InvalidFullNameError("x").reason == "x"
    assert accounts.WeakPasswordError("y").reason == "y"
    assert accounts.EmailAlreadyRegisteredError("a@b.pe").email == "a@b.pe"
    assert accounts.UserNotFoundError(4).user_id == 4
    assert accounts.RestaurantAlreadyHasStaffError(3).restaurant_id == 3
    assert accounts.RoleNotFoundError(8).role_id == 8
    assert accounts.InvalidRoleNameError("z").reason == "z"
    assert accounts.RoleNameTakenError("Cocinero").name == "Cocinero"
    assert accounts.BaseRoleNameFixedError("Mesero").name == "Mesero"
    assert accounts.BaseRoleNotDeletableError("Mesero").name == "Mesero"
    assert accounts.RoleInUseError("Cocinero").name == "Cocinero"
    assert accounts.CannotGrantPermissionsError(
        frozenset({Permission.CASH_MANAGE.value})
    ).missing == {Permission.CASH_MANAGE.value}
    assert accounts.NotASandboxAccountError(5).user_id == 5


def test_inventory_errors_keep_their_data() -> None:
    assert inventory.InvalidIngredientError("a").reason == "a"
    assert inventory.InvalidMovementError("b").reason == "b"
    assert inventory.InvalidRecipeError("c").reason == "c"
    assert inventory.IngredientNotFoundError(2).ingredient_id == 2
    assert inventory.IngredientNameTakenError("Arroz").name == "Arroz"
    assert inventory.IngredientInactiveError("Arroz").name == "Arroz"
    assert inventory.DishNotFoundError(9).menu_item_id == 9
    assert inventory.InvalidSupplierError("d").reason == "d"
    assert inventory.SupplierNotFoundError(6).supplier_id == 6
    assert inventory.SupplierNameTakenError("Makro").name == "Makro"
    assert inventory.InvalidPurchaseOrderError("e").reason == "e"
    assert inventory.PurchaseOrderNotFoundError(7).order_id == 7
