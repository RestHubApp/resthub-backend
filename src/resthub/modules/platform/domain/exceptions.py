"""Errores de dominio de la administración del sistema.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations


class PlatformError(Exception):
    """Raíz de los errores del módulo de plataforma."""


class InvalidCredentials(PlatformError):
    """Correo, contraseña o cuenta desactivada: la respuesta es la misma para no enumerar."""

    def __init__(self) -> None:
        super().__init__("El correo o la contraseña no son correctos.")


class AdminUnavailable(PlatformError):
    def __init__(self, admin_id: int) -> None:
        super().__init__("La cuenta ya no está disponible.")
        self.admin_id = admin_id


class InvalidAccountData(PlatformError):
    """Correo, nombre o contraseña de una cuenta nueva que no cumplen las reglas."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EmailAlreadyRegistered(PlatformError):
    def __init__(self, email: str) -> None:
        super().__init__(f"Ya existe una cuenta con el correo {email!r}.")
        self.email = email


class InvalidRestaurantData(PlatformError):
    """Nombre, identificador corto o zona horaria que no cumplen las reglas."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SlugAlreadyTaken(PlatformError):
    def __init__(self, slug: str) -> None:
        super().__init__(f"Ya existe un restaurante con el identificador {slug!r}.")
        self.slug = slug


class RestaurantNotFound(PlatformError):
    def __init__(self, restaurant_id: int) -> None:
        super().__init__(f"No existe el restaurante {restaurant_id}.")
        self.restaurant_id = restaurant_id
