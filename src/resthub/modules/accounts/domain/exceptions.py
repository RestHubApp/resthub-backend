"""Errores de dominio de cuentas.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations

from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class AccountsError(Exception):
    """Raíz de los errores del módulo de cuentas."""

    code: ClassVar[ErrorCode]


class InvalidEmailError(AccountsError):
    code = ErrorCode.INVALID_EMAIL

    def __init__(self, value: str) -> None:
        super().__init__(f"El correo electrónico no es válido: {value!r}")
        self.value = value


class InvalidFullNameError(AccountsError):
    code = ErrorCode.INVALID_FULL_NAME

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WeakPasswordError(AccountsError):
    code = ErrorCode.WEAK_PASSWORD

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EmailAlreadyRegisteredError(AccountsError):
    code = ErrorCode.EMAIL_ALREADY_REGISTERED

    def __init__(self, email: str) -> None:
        super().__init__(f"Ya existe una cuenta con el correo {email!r}.")
        self.email = email


class UserNotFoundError(AccountsError):
    """También cuando la cuenta existe pero es de otro restaurante.

    Responder distinto ("existe, pero no es tuya") le diría a un encargado qué
    identificadores usa el resto de los locales.
    """

    code = ErrorCode.USER_NOT_FOUND

    def __init__(self, user_id: int) -> None:
        super().__init__(f"No existe la cuenta {user_id}.")
        self.user_id = user_id


class InvalidCredentialsError(AccountsError):
    """No se dice si falló el correo o la contraseña: eso enumera cuentas."""

    code = ErrorCode.INVALID_CREDENTIALS

    def __init__(self) -> None:
        super().__init__("El correo o la contraseña no son correctos.")


class InactiveRestaurantError(AccountsError):
    code = ErrorCode.INACTIVE_RESTAURANT

    def __init__(self) -> None:
        super().__init__("El restaurante de esta cuenta está desactivado.")


class WrongCurrentPasswordError(AccountsError):
    code = ErrorCode.WRONG_CURRENT_PASSWORD

    def __init__(self) -> None:
        super().__init__("La contraseña actual no es correcta.")


class CannotDeactivateSelfError(AccountsError):
    code = ErrorCode.CANNOT_DEACTIVATE_SELF

    def __init__(self) -> None:
        super().__init__("No puedes desactivar tu propia cuenta.")


class InvalidDeactivationReasonError(AccountsError):
    code = ErrorCode.INVALID_DEACTIVATION_REASON

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CannotChangeOwnRoleError(AccountsError):
    code = ErrorCode.CANNOT_CHANGE_OWN_ROLE

    def __init__(self) -> None:
        super().__init__("No puedes cambiar tu propio rol.")


class CannotResetOwnPasswordError(AccountsError):
    code = ErrorCode.CANNOT_RESET_OWN_PASSWORD

    def __init__(self) -> None:
        super().__init__("Para tu propia cuenta usa el cambio de contraseña, que pide la actual.")


class RestaurantAlreadyHasStaffError(AccountsError):
    code = ErrorCode.RESTAURANT_ALREADY_HAS_STAFF

    def __init__(self, restaurant_id: int) -> None:
        super().__init__(f"El restaurante {restaurant_id} ya tiene personal registrado.")
        self.restaurant_id = restaurant_id


class CannotManageStrongerAccountError(AccountsError):
    """Evita que alguien con `staff.manage` tome o degrade una cuenta con más poder.

    Sin esta regla, un rol con `staff.manage` pero sin el resto podría
    restablecer la contraseña del encargado y entrar como él.
    """

    code = ErrorCode.CANNOT_MANAGE_STRONGER_ACCOUNT

    def __init__(self) -> None:
        super().__init__("No puedes gestionar una cuenta que tiene permisos que tú no tienes.")


class RoleNotFoundError(AccountsError):
    """También cuando el rol existe pero es de otro restaurante."""

    code = ErrorCode.ROLE_NOT_FOUND

    def __init__(self, role_id: int) -> None:
        super().__init__(f"No existe el rol {role_id}.")
        self.role_id = role_id


class InvalidRoleNameError(AccountsError):
    code = ErrorCode.INVALID_ROLE_NAME

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RoleNameTakenError(AccountsError):
    code = ErrorCode.ROLE_NAME_TAKEN

    def __init__(self, name: str) -> None:
        super().__init__(f"Ya existe un rol llamado {name!r}.")
        self.name = name


class RoleNotEditableError(AccountsError):
    code = ErrorCode.ROLE_NOT_EDITABLE

    def __init__(self) -> None:
        super().__init__("El rol de encargado no se edita: siempre tiene todos los permisos.")


class BaseRoleNameFixedError(AccountsError):
    code = ErrorCode.BASE_ROLE_NAME_FIXED

    def __init__(self, name: str) -> None:
        super().__init__(f"El rol {name} no cambia de nombre; solo sus permisos.")
        self.name = name


class BaseRoleNotDeletableError(AccountsError):
    code = ErrorCode.BASE_ROLE_NOT_DELETABLE

    def __init__(self, name: str) -> None:
        super().__init__(f"El rol {name} es de todo restaurante y no se elimina.")
        self.name = name


class RoleInUseError(AccountsError):
    code = ErrorCode.ROLE_IN_USE

    def __init__(self, name: str) -> None:
        super().__init__(f"El rol {name} tiene personal asignado; cámbialo de rol antes.")
        self.name = name


class CannotGrantPermissionsError(AccountsError):
    """Nadie reparte lo que no tiene: si no, `roles.manage` alcanzaría para todo."""

    code = ErrorCode.CANNOT_GRANT_PERMISSIONS

    def __init__(self, missing: frozenset[str]) -> None:
        super().__init__("No puedes dar permisos que no tienes.")
        self.missing = missing


class CannotManageStrongerRoleError(AccountsError):
    code = ErrorCode.CANNOT_MANAGE_STRONGER_ROLE

    def __init__(self) -> None:
        super().__init__("No puedes cambiar un rol que tiene permisos que tú no tienes.")


class InvalidPreviewCodeError(AccountsError):
    """Inexistente, vencido o ya usado: la respuesta es la misma para no dar pistas."""

    code = ErrorCode.INVALID_PREVIEW_CODE

    def __init__(self) -> None:
        super().__init__("El código de vista previa no es válido o ya venció.")


class NotASandboxAccountError(AccountsError):
    """La vista previa solo entra a cuentas activas del local de muestra."""

    code = ErrorCode.NOT_A_SANDBOX_ACCOUNT

    def __init__(self, user_id: int) -> None:
        super().__init__(f"La cuenta {user_id} no es del local de muestra.")
        self.user_id = user_id


class PreviewSessionRestrictedError(AccountsError):
    code = ErrorCode.PREVIEW_SESSION_RESTRICTED

    def __init__(self) -> None:
        super().__init__("En la vista previa no se cambia la contraseña.")


class OutdatedTermsError(AccountsError):
    code = ErrorCode.OUTDATED_TERMS

    def __init__(self, version: str) -> None:
        super().__init__(
            "Los textos cambiaron desde que los abriste. Vuelve a leerlos y acéptalos de nuevo."
        )
        self.version = version


class InvalidPinError(AccountsError):
    code = ErrorCode.INVALID_PIN

    def __init__(self) -> None:
        super().__init__("El PIN tiene de 4 a 6 dígitos.")


class WrongPinError(AccountsError):
    code = ErrorCode.WRONG_PIN

    def __init__(self, remaining: int) -> None:
        attempts = "intento" if remaining == 1 else "intentos"
        super().__init__(f"El PIN no es correcto. Te quedan {remaining} {attempts}.")
        self.remaining = remaining


class PinNotSetError(AccountsError):
    code = ErrorCode.PIN_NOT_SET

    def __init__(self) -> None:
        super().__init__("La cuenta no tiene PIN: desbloquea con tu contraseña.")
