"""Errores de dominio de cuentas.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations


class AccountsError(Exception):
    """Raíz de los errores del módulo de cuentas."""


class InvalidEmail(AccountsError):
    def __init__(self, value: str) -> None:
        super().__init__(f"El correo electrónico no es válido: {value!r}")
        self.value = value


class InvalidFullName(AccountsError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WeakPassword(AccountsError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EmailAlreadyRegistered(AccountsError):
    def __init__(self, email: str) -> None:
        super().__init__(f"Ya existe una cuenta con el correo {email!r}.")
        self.email = email


class UserNotFound(AccountsError):
    """También cuando la cuenta existe pero es de otro restaurante.

    Responder distinto ("existe, pero no es tuya") le diría a un encargado qué
    identificadores usa el resto de los locales.
    """

    def __init__(self, user_id: int) -> None:
        super().__init__(f"No existe la cuenta {user_id}.")
        self.user_id = user_id


class InvalidCredentials(AccountsError):
    """No se dice si falló el correo o la contraseña: eso enumera cuentas."""

    def __init__(self) -> None:
        super().__init__("El correo o la contraseña no son correctos.")


class InactiveAccount(AccountsError):
    def __init__(self, email: str) -> None:
        super().__init__(f"La cuenta {email!r} está desactivada.")
        self.email = email


class InactiveRestaurant(AccountsError):
    def __init__(self) -> None:
        super().__init__("El restaurante de esta cuenta está desactivado.")


class WrongCurrentPassword(AccountsError):
    def __init__(self) -> None:
        super().__init__("La contraseña actual no es correcta.")


class CannotDeactivateSelf(AccountsError):
    def __init__(self) -> None:
        super().__init__("No puedes desactivar tu propia cuenta.")


class CannotChangeOwnRole(AccountsError):
    def __init__(self) -> None:
        super().__init__("No puedes cambiar tu propio rol.")


class CannotResetOwnPassword(AccountsError):
    def __init__(self) -> None:
        super().__init__("Para tu propia cuenta usa el cambio de contraseña, que pide la actual.")


class RestaurantAlreadyHasStaff(AccountsError):
    def __init__(self, restaurant_id: int) -> None:
        super().__init__(f"El restaurante {restaurant_id} ya tiene personal registrado.")
        self.restaurant_id = restaurant_id


class CannotManageStrongerAccount(AccountsError):
    """Evita que alguien con `staff.manage` tome o degrade una cuenta con más poder.

    Sin esta regla, un rol con `staff.manage` pero sin el resto podría
    restablecer la contraseña del encargado y entrar como él.
    """

    def __init__(self) -> None:
        super().__init__("No puedes gestionar una cuenta que tiene permisos que tú no tienes.")


class RoleNotFound(AccountsError):
    """También cuando el rol existe pero es de otro restaurante."""

    def __init__(self, role_id: int) -> None:
        super().__init__(f"No existe el rol {role_id}.")
        self.role_id = role_id


class InvalidRoleName(AccountsError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RoleNameTaken(AccountsError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Ya existe un rol llamado {name!r}.")
        self.name = name


class RoleNotEditable(AccountsError):
    def __init__(self) -> None:
        super().__init__("El rol de encargado no se edita: siempre tiene todos los permisos.")


class BaseRoleNameFixed(AccountsError):
    def __init__(self, name: str) -> None:
        super().__init__(f"El rol {name} no cambia de nombre; solo sus permisos.")
        self.name = name


class BaseRoleNotDeletable(AccountsError):
    def __init__(self, name: str) -> None:
        super().__init__(f"El rol {name} es de todo restaurante y no se elimina.")
        self.name = name


class RoleInUse(AccountsError):
    def __init__(self, name: str) -> None:
        super().__init__(f"El rol {name} tiene personal asignado; cámbialo de rol antes.")
        self.name = name


class CannotGrantPermissions(AccountsError):
    """Nadie reparte lo que no tiene: si no, `roles.manage` alcanzaría para todo."""

    def __init__(self, missing: frozenset[str]) -> None:
        super().__init__("No puedes dar permisos que no tienes.")
        self.missing = missing


class CannotManageStrongerRole(AccountsError):
    def __init__(self) -> None:
        super().__init__("No puedes cambiar un rol que tiene permisos que tú no tienes.")
