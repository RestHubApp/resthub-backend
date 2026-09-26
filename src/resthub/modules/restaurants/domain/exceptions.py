"""Errores de dominio de restaurantes.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations


class RestaurantsError(Exception):
    """Raíz de los errores del módulo de restaurantes."""


class InvalidRestaurantName(RestaurantsError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidSlug(RestaurantsError):
    def __init__(self, value: str) -> None:
        super().__init__(
            f"El identificador {value!r} no es válido. Usa minúsculas, números y guiones, "
            "sin guiones al principio ni al final."
        )
        self.value = value


class InvalidTimezone(RestaurantsError):
    def __init__(self, value: str) -> None:
        super().__init__(f"La zona horaria {value!r} no existe. Ejemplo: America/Lima.")
        self.value = value


class InvalidDiscountLimit(RestaurantsError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RestaurantNotFound(RestaurantsError):
    def __init__(self, restaurant_id: int) -> None:
        super().__init__(f"No existe el restaurante {restaurant_id}.")
        self.restaurant_id = restaurant_id


class SlugAlreadyTaken(RestaurantsError):
    def __init__(self, slug: str) -> None:
        super().__init__(f"Ya existe un restaurante con el identificador {slug!r}.")
        self.slug = slug
