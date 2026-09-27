"""Errores de dominio de cuentas e inventario: el mensaje que ve la persona y sus datos.

El API responde con el texto del error (`detail`), así que ese texto es parte
del comportamiento: decirle a un encargado «No puedes cambiar tu propio rol» o
cuál es el insumo desactivado es lo que le permite corregir la operación.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from resthub.modules.accounts.domain import exceptions as cuentas
from resthub.modules.inventory.domain import exceptions as inventario


@pytest.mark.parametrize(
    ("fabrica", "mensaje"),
    [
        (lambda: cuentas.InvalidEmail("ana@"), "El correo electrónico no es válido: 'ana@'"),
        (lambda: cuentas.InvalidFullName("Falta el nombre."), "Falta el nombre."),
        (lambda: cuentas.WeakPassword("Muy corta."), "Muy corta."),
        (
            lambda: cuentas.EmailAlreadyRegistered("ana@rosa.pe"),
            "Ya existe una cuenta con el correo 'ana@rosa.pe'.",
        ),
        (lambda: cuentas.UserNotFound(4), "No existe la cuenta 4."),
        (lambda: cuentas.InvalidCredentials(), "El correo o la contraseña no son correctos."),
        (
            lambda: cuentas.InactiveAccount("ana@rosa.pe"),
            "La cuenta 'ana@rosa.pe' está desactivada.",
        ),
        (lambda: cuentas.InactiveRestaurant(), "El restaurante de esta cuenta está desactivado."),
        (lambda: cuentas.WrongCurrentPassword(), "La contraseña actual no es correcta."),
        (lambda: cuentas.CannotDeactivateSelf(), "No puedes desactivar tu propia cuenta."),
        (lambda: cuentas.CannotChangeOwnRole(), "No puedes cambiar tu propio rol."),
        (
            lambda: cuentas.CannotResetOwnPassword(),
            "Para tu propia cuenta usa el cambio de contraseña, que pide la actual.",
        ),
        (
            lambda: cuentas.RestaurantAlreadyHasStaff(3),
            "El restaurante 3 ya tiene personal registrado.",
        ),
        (
            lambda: cuentas.CannotManageStrongerAccount(),
            "No puedes gestionar una cuenta que tiene permisos que tú no tienes.",
        ),
        (lambda: cuentas.RoleNotFound(8), "No existe el rol 8."),
        (lambda: cuentas.InvalidRoleName("Nombre vacío."), "Nombre vacío."),
        (lambda: cuentas.RoleNameTaken("Cocinero"), "Ya existe un rol llamado 'Cocinero'."),
        (
            lambda: cuentas.RoleNotEditable(),
            "El rol de encargado no se edita: siempre tiene todos los permisos.",
        ),
        (
            lambda: cuentas.BaseRoleNameFixed("Mesero"),
            "El rol Mesero no cambia de nombre; solo sus permisos.",
        ),
        (
            lambda: cuentas.BaseRoleNotDeletable("Mesero"),
            "El rol Mesero es de todo restaurante y no se elimina.",
        ),
        (
            lambda: cuentas.RoleInUse("Cocinero"),
            "El rol Cocinero tiene personal asignado; cámbialo de rol antes.",
        ),
        (
            lambda: cuentas.CannotGrantPermissions(frozenset({"cash.manage"})),
            "No puedes dar permisos que no tienes.",
        ),
        (
            lambda: cuentas.CannotManageStrongerRole(),
            "No puedes cambiar un rol que tiene permisos que tú no tienes.",
        ),
        (
            lambda: cuentas.InvalidPreviewCode(),
            "El código de vista previa no es válido o ya venció.",
        ),
        (lambda: cuentas.NotASandboxAccount(5), "La cuenta 5 no es del local de muestra."),
        (
            lambda: cuentas.PreviewSessionRestricted(),
            "En la vista previa no se cambia la contraseña.",
        ),
        (lambda: inventario.InvalidIngredient("Sin nombre."), "Sin nombre."),
        (lambda: inventario.InvalidMovement("Resta."), "Resta."),
        (lambda: inventario.InvalidRecipe("Repetido."), "Repetido."),
        (lambda: inventario.IngredientNotFound(2), "No existe el insumo 2."),
        (lambda: inventario.IngredientNameTaken("Arroz"), "Ya existe un insumo llamado 'Arroz'."),
        (lambda: inventario.IngredientInactive("Arroz"), "El insumo Arroz está desactivado."),
        (lambda: inventario.DishNotFound(9), "No existe el plato 9."),
        (lambda: inventario.InvalidSupplier("Sin nombre."), "Sin nombre."),
        (lambda: inventario.SupplierNotFound(6), "No existe el proveedor 6."),
        (lambda: inventario.SupplierNameTaken("Makro"), "Ya existe un proveedor llamado 'Makro'."),
        (lambda: inventario.InvalidPurchaseOrder("Vacía."), "Vacía."),
        (lambda: inventario.PurchaseOrderNotFound(7), "No existe la orden de compra 7."),
    ],
)
def test_cada_error_explica_lo_que_paso(fabrica: Callable[[], Exception], mensaje: str) -> None:
    assert str(fabrica()) == mensaje


def test_los_errores_de_cuentas_guardan_sus_datos() -> None:
    assert cuentas.InvalidEmail("ana@").value == "ana@"
    assert cuentas.InvalidFullName("x").reason == "x"
    assert cuentas.WeakPassword("y").reason == "y"
    assert cuentas.EmailAlreadyRegistered("a@b.pe").email == "a@b.pe"
    assert cuentas.UserNotFound(4).user_id == 4
    assert cuentas.InactiveAccount("a@b.pe").email == "a@b.pe"
    assert cuentas.RestaurantAlreadyHasStaff(3).restaurant_id == 3
    assert cuentas.RoleNotFound(8).role_id == 8
    assert cuentas.InvalidRoleName("z").reason == "z"
    assert cuentas.RoleNameTaken("Cocinero").name == "Cocinero"
    assert cuentas.BaseRoleNameFixed("Mesero").name == "Mesero"
    assert cuentas.BaseRoleNotDeletable("Mesero").name == "Mesero"
    assert cuentas.RoleInUse("Cocinero").name == "Cocinero"
    assert cuentas.CannotGrantPermissions(frozenset({"cash.manage"})).missing == {"cash.manage"}
    assert cuentas.NotASandboxAccount(5).user_id == 5


def test_los_errores_de_inventario_guardan_sus_datos() -> None:
    assert inventario.InvalidIngredient("a").reason == "a"
    assert inventario.InvalidMovement("b").reason == "b"
    assert inventario.InvalidRecipe("c").reason == "c"
    assert inventario.IngredientNotFound(2).ingredient_id == 2
    assert inventario.IngredientNameTaken("Arroz").name == "Arroz"
    assert inventario.IngredientInactive("Arroz").name == "Arroz"
    assert inventario.DishNotFound(9).menu_item_id == 9
    assert inventario.InvalidSupplier("d").reason == "d"
    assert inventario.SupplierNotFound(6).supplier_id == 6
    assert inventario.SupplierNameTaken("Makro").name == "Makro"
    assert inventario.InvalidPurchaseOrder("e").reason == "e"
    assert inventario.PurchaseOrderNotFound(7).order_id == 7
