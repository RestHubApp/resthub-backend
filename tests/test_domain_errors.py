"""Errores de dominio de cuentas e inventario: el mensaje que ve la persona y sus datos.

El API responde con el texto del error (`detail`), así que ese texto es parte
del comportamiento: decirle a un encargado «No puedes cambiar tu propio rol» o
cuál es el insumo desactivado es lo que le permite corregir la operación.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from resthub.core.permissions import Permission
from resthub.modules.accounts.domain import exceptions as cuentas
from resthub.modules.inventory.domain import exceptions as inventario


@pytest.mark.parametrize(
    ("fabrica", "mensaje"),
    [
        (lambda: cuentas.InvalidEmailError("ana@"), "El correo electrónico no es válido: 'ana@'"),
        (lambda: cuentas.InvalidFullNameError("Falta el nombre."), "Falta el nombre."),
        (lambda: cuentas.WeakPasswordError("Muy corta."), "Muy corta."),
        (
            lambda: cuentas.EmailAlreadyRegisteredError("ana@rosa.pe"),
            "Ya existe una cuenta con el correo 'ana@rosa.pe'.",
        ),
        (lambda: cuentas.UserNotFoundError(4), "No existe la cuenta 4."),
        (lambda: cuentas.InvalidCredentialsError(), "El correo o la contraseña no son correctos."),
        (
            lambda: cuentas.InactiveRestaurantError(),
            "El restaurante de esta cuenta está desactivado.",
        ),
        (lambda: cuentas.WrongCurrentPasswordError(), "La contraseña actual no es correcta."),
        (lambda: cuentas.CannotDeactivateSelfError(), "No puedes desactivar tu propia cuenta."),
        (lambda: cuentas.CannotChangeOwnRoleError(), "No puedes cambiar tu propio rol."),
        (
            lambda: cuentas.CannotResetOwnPasswordError(),
            "Para tu propia cuenta usa el cambio de contraseña, que pide la actual.",
        ),
        (
            lambda: cuentas.RestaurantAlreadyHasStaffError(3),
            "El restaurante 3 ya tiene personal registrado.",
        ),
        (
            lambda: cuentas.CannotManageStrongerAccountError(),
            "No puedes gestionar una cuenta que tiene permisos que tú no tienes.",
        ),
        (lambda: cuentas.RoleNotFoundError(8), "No existe el rol 8."),
        (lambda: cuentas.InvalidRoleNameError("Nombre vacío."), "Nombre vacío."),
        (lambda: cuentas.RoleNameTakenError("Cocinero"), "Ya existe un rol llamado 'Cocinero'."),
        (
            lambda: cuentas.RoleNotEditableError(),
            "El rol de encargado no se edita: siempre tiene todos los permisos.",
        ),
        (
            lambda: cuentas.BaseRoleNameFixedError("Mesero"),
            "El rol Mesero no cambia de nombre; solo sus permisos.",
        ),
        (
            lambda: cuentas.BaseRoleNotDeletableError("Mesero"),
            "El rol Mesero es de todo restaurante y no se elimina.",
        ),
        (
            lambda: cuentas.RoleInUseError("Cocinero"),
            "El rol Cocinero tiene personal asignado; cámbialo de rol antes.",
        ),
        (
            lambda: cuentas.CannotGrantPermissionsError(frozenset({Permission.CASH_MANAGE.value})),
            "No puedes dar permisos que no tienes.",
        ),
        (
            lambda: cuentas.CannotManageStrongerRoleError(),
            "No puedes cambiar un rol que tiene permisos que tú no tienes.",
        ),
        (
            lambda: cuentas.InvalidPreviewCodeError(),
            "El código de vista previa no es válido o ya venció.",
        ),
        (lambda: cuentas.NotASandboxAccountError(5), "La cuenta 5 no es del local de muestra."),
        (
            lambda: cuentas.PreviewSessionRestrictedError(),
            "En la vista previa no se cambia la contraseña.",
        ),
        (lambda: inventario.InvalidIngredientError("Sin nombre."), "Sin nombre."),
        (lambda: inventario.InvalidMovementError("Resta."), "Resta."),
        (lambda: inventario.InvalidRecipeError("Repetido."), "Repetido."),
        (lambda: inventario.IngredientNotFoundError(2), "No existe el insumo 2."),
        (
            lambda: inventario.IngredientNameTakenError("Arroz"),
            "Ya existe un insumo llamado 'Arroz'.",
        ),
        (lambda: inventario.IngredientInactiveError("Arroz"), "El insumo Arroz está desactivado."),
        (lambda: inventario.DishNotFoundError(9), "No existe el plato 9."),
        (lambda: inventario.InvalidSupplierError("Sin nombre."), "Sin nombre."),
        (lambda: inventario.SupplierNotFoundError(6), "No existe el proveedor 6."),
        (
            lambda: inventario.SupplierNameTakenError("Makro"),
            "Ya existe un proveedor llamado 'Makro'.",
        ),
        (lambda: inventario.InvalidPurchaseOrderError("Vacía."), "Vacía."),
        (lambda: inventario.PurchaseOrderNotFoundError(7), "No existe la orden de compra 7."),
    ],
)
def test_cada_error_explica_lo_que_paso(fabrica: Callable[[], Exception], mensaje: str) -> None:
    assert str(fabrica()) == mensaje


def test_los_errores_de_cuentas_guardan_sus_datos() -> None:
    assert cuentas.InvalidEmailError("ana@").value == "ana@"
    assert cuentas.InvalidFullNameError("x").reason == "x"
    assert cuentas.WeakPasswordError("y").reason == "y"
    assert cuentas.EmailAlreadyRegisteredError("a@b.pe").email == "a@b.pe"
    assert cuentas.UserNotFoundError(4).user_id == 4
    assert cuentas.RestaurantAlreadyHasStaffError(3).restaurant_id == 3
    assert cuentas.RoleNotFoundError(8).role_id == 8
    assert cuentas.InvalidRoleNameError("z").reason == "z"
    assert cuentas.RoleNameTakenError("Cocinero").name == "Cocinero"
    assert cuentas.BaseRoleNameFixedError("Mesero").name == "Mesero"
    assert cuentas.BaseRoleNotDeletableError("Mesero").name == "Mesero"
    assert cuentas.RoleInUseError("Cocinero").name == "Cocinero"
    assert cuentas.CannotGrantPermissionsError(
        frozenset({Permission.CASH_MANAGE.value})
    ).missing == {Permission.CASH_MANAGE.value}
    assert cuentas.NotASandboxAccountError(5).user_id == 5


def test_los_errores_de_inventario_guardan_sus_datos() -> None:
    assert inventario.InvalidIngredientError("a").reason == "a"
    assert inventario.InvalidMovementError("b").reason == "b"
    assert inventario.InvalidRecipeError("c").reason == "c"
    assert inventario.IngredientNotFoundError(2).ingredient_id == 2
    assert inventario.IngredientNameTakenError("Arroz").name == "Arroz"
    assert inventario.IngredientInactiveError("Arroz").name == "Arroz"
    assert inventario.DishNotFoundError(9).menu_item_id == 9
    assert inventario.InvalidSupplierError("d").reason == "d"
    assert inventario.SupplierNotFoundError(6).supplier_id == 6
    assert inventario.SupplierNameTakenError("Makro").name == "Makro"
    assert inventario.InvalidPurchaseOrderError("e").reason == "e"
    assert inventario.PurchaseOrderNotFoundError(7).order_id == 7
