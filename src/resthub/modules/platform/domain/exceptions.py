"""Errores de dominio de la administración del sistema.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations


class PlatformError(Exception):
    """Raíz de los errores del módulo de plataforma."""


class InvalidCredentialsError(PlatformError):
    """Correo, contraseña o cuenta desactivada: la respuesta es la misma para no enumerar."""

    def __init__(self) -> None:
        super().__init__("El correo o la contraseña no son correctos.")


class AdminUnavailableError(PlatformError):
    def __init__(self, admin_id: int) -> None:
        super().__init__("La cuenta ya no está disponible.")
        self.admin_id = admin_id


class InvalidAccountDataError(PlatformError):
    """Correo, nombre o contraseña de una cuenta nueva que no cumplen las reglas."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EmailAlreadyRegisteredError(PlatformError):
    def __init__(self, email: str) -> None:
        super().__init__(f"Ya existe una cuenta con el correo {email!r}.")
        self.email = email


class InvalidRestaurantDataError(PlatformError):
    """Nombre, identificador corto o zona horaria que no cumplen las reglas."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SlugAlreadyTakenError(PlatformError):
    def __init__(self, slug: str) -> None:
        super().__init__(f"Ya existe un restaurante con el identificador {slug!r}.")
        self.slug = slug


class RestaurantNotFoundError(PlatformError):
    def __init__(self, restaurant_id: int) -> None:
        super().__init__(f"No existe el restaurante {restaurant_id}.")
        self.restaurant_id = restaurant_id


class SandboxAccountUnavailableError(PlatformError):
    """El local de muestra no tiene una cuenta activa de ese tipo: la cambiaron desde adentro."""

    def __init__(self, kind: str) -> None:
        super().__init__(
            "El local de muestra no tiene una cuenta activa de ese tipo. "
            "Reinícialo para recuperarla."
        )
        self.kind = kind


class SandboxAlreadyActiveError(PlatformError):
    """Otro pedido creó el local de muestra a la vez: ya hay uno vigente."""

    def __init__(self) -> None:
        super().__init__("Ya hay un local de muestra vigente. Vuelve a intentarlo.")


class LogEntryNotFoundError(PlatformError):
    def __init__(self, entry_id: int) -> None:
        super().__init__(f"No existe la entrada de log {entry_id}.")
        self.entry_id = entry_id
