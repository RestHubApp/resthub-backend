"""Traducción de los errores del módulo a HTTP."""

from __future__ import annotations

from fastapi import HTTPException, status

from resthub.core.errors import api_error
from resthub.modules.platform.domain.exceptions import (
    EmailAlreadyRegisteredError,
    InvalidAccountDataError,
    InvalidRestaurantDataError,
    LogEntryNotFoundError,
    PlatformError,
    RestaurantNotFoundError,
    SandboxAccountUnavailableError,
    SandboxAlreadyActiveError,
    SlugAlreadyTakenError,
)

_STATUS: tuple[tuple[type[PlatformError], int], ...] = (
    (RestaurantNotFoundError, status.HTTP_404_NOT_FOUND),
    (LogEntryNotFoundError, status.HTTP_404_NOT_FOUND),
    (SlugAlreadyTakenError, status.HTTP_409_CONFLICT),
    (EmailAlreadyRegisteredError, status.HTTP_409_CONFLICT),
    (InvalidRestaurantDataError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (InvalidAccountDataError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (SandboxAccountUnavailableError, status.HTTP_409_CONFLICT),
    (SandboxAlreadyActiveError, status.HTTP_409_CONFLICT),
)


def to_http(error: PlatformError) -> HTTPException:
    for kind, code in _STATUS:
        if isinstance(error, kind):
            return api_error(code, error)
    # Un error de dominio sin traducción es un error de programación: que se vea.
    raise error
