"""Las respuestas de error del API: una sola forma, `{"detail": ..., "code": ...}`.

`detail` es el texto en español para la persona (o, en un 422 de validación, la lista de
FastAPI, como siempre). `code` es el `ErrorCode` estable para el programa. Es un cambio
aditivo: quien solo lee `detail` no nota nada.

Los routers no arman `HTTPException` a mano: traducen una excepción de dominio con
`api_error(status, error)`, que toma el `code` de la propia excepción, o levantan
`ApiError(status, texto, ErrorCode.X)`. Un `HTTPException` suelto (el 404 de una ruta que
no existe, el 405) se completa aquí con el código de su estado.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from resthub.core.error_codes import ErrorCode

# Los códigos genéricos de un `HTTPException` que no declaró el suyo, según su estado.
_CODE_BY_STATUS: dict[int, ErrorCode] = {
    status.HTTP_400_BAD_REQUEST: ErrorCode.BAD_REQUEST,
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHENTICATED,
    status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    status.HTTP_409_CONFLICT: ErrorCode.CONFLICT,
    status.HTTP_422_UNPROCESSABLE_CONTENT: ErrorCode.UNPROCESSABLE,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
}
# Estados que no llevan cuerpo (RFC 9110): no se les escribe `detail` ni `code`.
_BODILESS = frozenset({status.HTTP_204_NO_CONTENT, status.HTTP_304_NOT_MODIFIED})


class CodedError(Protocol):
    """Una excepción de dominio: su mensaje es el `detail` y su `code` es el código."""

    @property
    def code(self) -> ErrorCode: ...


class ApiError(HTTPException):
    """Un `HTTPException` que lleva el `ErrorCode` de la respuesta."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        code: ErrorCode,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code, detail, headers=dict(headers) if headers else None)
        self.code = code


def api_error(
    status_code: int, error: CodedError, headers: Mapping[str, str] | None = None
) -> ApiError:
    """Traduce una excepción de dominio: su mensaje es el `detail`, su `code`, el `code`."""
    return ApiError(status_code, str(error), error.code, headers)


def error_body(detail: object, code: ErrorCode) -> dict[str, Any]:
    return {"detail": detail, "code": code.value}


def error_response(
    status_code: int,
    detail: str,
    code: ErrorCode,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Una respuesta de error ya armada, para los middlewares que no pasan por FastAPI."""
    return JSONResponse(
        error_body(detail, code),
        status_code=status_code,
        headers=dict(headers) if headers else None,
    )


async def _http_exception(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, StarletteHTTPException):  # pragma: no cover - lo garantiza el registro
        raise error
    headers = error.headers
    if error.status_code in _BODILESS:
        return JSONResponse(None, status_code=error.status_code, headers=headers)
    code = (
        error.code
        if isinstance(error, ApiError)
        else _CODE_BY_STATUS.get(error.status_code, ErrorCode.HTTP_ERROR)
    )
    return JSONResponse(
        error_body(error.detail, code), status_code=error.status_code, headers=headers
    )


async def _validation_error(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, RequestValidationError):  # pragma: no cover - lo garantiza el registro
        raise error
    # `detail` conserva la lista de FastAPI (el frontend lee `detail[0].msg`); `errors` es la
    # misma lista bajo una clave propia y documentada, para no depender de que `detail`
    # vuelva a ser un texto algún día.
    errors = jsonable_encoder(error.errors())
    body = error_body(errors, ErrorCode.VALIDATION_ERROR)
    body["errors"] = errors
    return JSONResponse(body, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
