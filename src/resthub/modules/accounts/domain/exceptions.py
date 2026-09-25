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
