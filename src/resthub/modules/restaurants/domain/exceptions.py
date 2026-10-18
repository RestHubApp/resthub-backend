"""Errores de dominio de restaurantes.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations

from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class RestaurantsError(Exception):
    """Raíz de los errores del módulo de restaurantes."""

    code: ClassVar[ErrorCode]


class InvalidRestaurantNameError(RestaurantsError):
    code = ErrorCode.INVALID_RESTAURANT_NAME

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidSlugError(RestaurantsError):
    code = ErrorCode.INVALID_SLUG

    def __init__(self, value: str) -> None:
        super().__init__(
            f"El identificador {value!r} no es válido. Usa minúsculas, números y guiones, "
            "sin guiones al principio ni al final."
        )
        self.value = value


class InvalidTimezoneError(RestaurantsError):
    code = ErrorCode.INVALID_TIMEZONE

    def __init__(self, value: str) -> None:
        super().__init__(f"La zona horaria {value!r} no existe. Ejemplo: America/Lima.")
        self.value = value


class InvalidDiscountLimitError(RestaurantsError):
    code = ErrorCode.INVALID_DISCOUNT_LIMIT

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RestaurantNotFoundError(RestaurantsError):
    code = ErrorCode.RESTAURANT_NOT_FOUND

    def __init__(self, restaurant_id: int) -> None:
        super().__init__(f"No existe el restaurante {restaurant_id}.")
        self.restaurant_id = restaurant_id


class SlugAlreadyTakenError(RestaurantsError):
    code = ErrorCode.SLUG_ALREADY_TAKEN

    def __init__(self, slug: str) -> None:
        super().__init__(f"Ya existe un restaurante con el identificador {slug!r}.")
        self.slug = slug


class ReservedSlugError(RestaurantsError):
    """Los identificadores que empiezan con `muestra` son del local de muestra."""

    code = ErrorCode.RESERVED_SLUG

    def __init__(self, slug: str) -> None:
        super().__init__(
            f"El identificador {slug!r} está reservado para el local de muestra. Elige otro."
        )
        self.slug = slug


class SandboxAlreadyActiveError(RestaurantsError):
    """Ya hay un local de muestra vigente: otro pedido lo creó a la vez."""

    code = ErrorCode.SANDBOX_ALREADY_ACTIVE

    def __init__(self) -> None:
        super().__init__("Ya hay un local de muestra vigente. Vuelve a intentarlo.")


class InvalidWasteApprovalThresholdError(RestaurantsError):
    code = ErrorCode.INVALID_WASTE_APPROVAL_THRESHOLD

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidOpeningHoursError(RestaurantsError):
    code = ErrorCode.INVALID_OPENING_HOURS

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidIdleLockError(RestaurantsError):
    code = ErrorCode.INVALID_IDLE_LOCK

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
