"""Traducción de los errores del módulo a HTTP."""

from __future__ import annotations

from fastapi import HTTPException, status

from resthub.modules.platform.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidAccountData,
    InvalidRestaurantData,
    PlatformError,
    RestaurantNotFound,
    SlugAlreadyTaken,
)

_STATUS: tuple[tuple[type[PlatformError], int], ...] = (
    (RestaurantNotFound, status.HTTP_404_NOT_FOUND),
    (SlugAlreadyTaken, status.HTTP_409_CONFLICT),
    (EmailAlreadyRegistered, status.HTTP_409_CONFLICT),
    (InvalidRestaurantData, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (InvalidAccountData, status.HTTP_422_UNPROCESSABLE_CONTENT),
)


def to_http(error: PlatformError) -> HTTPException:
    for kind, code in _STATUS:
        if isinstance(error, kind):
            return HTTPException(code, str(error))
    # Un error de dominio sin traducción es un error de programación: que se vea.
    raise error
