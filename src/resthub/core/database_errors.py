"""Qué responde el API cuando la base de datos no está.

Sin esto, una base caída o inalcanzable llega al cliente como un 500 genérico
en texto plano («Internal Server Error»), indistinguible de un error de
programación, o como una conexión cortada sin respuesta. Con esto es un 503 en
JSON, con un mensaje y `Retry-After`: el frontend lo trata como «sin conexión
con el servidor» y el pedido va a la cola del celular, que se reintenta solo y
no se duplica gracias a `client_request_id` (experimento de caos 01).

Solo se traduce lo que indica que la base no está disponible: una conexión
cortada o rechazada, un plazo vencido o el pool agotado. Un error de SQL o de
integridad sigue siendo un 500 con su traza, porque es un error del programa.
"""

from __future__ import annotations

import traceback

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from resthub.core.logs import get_logger

DATABASE_UNAVAILABLE_DETAIL = (
    "No hay conexión con la base de datos. Intenta de nuevo en unos segundos."
)
RETRY_AFTER_SECONDS = 5

# Los errores de asyncpg que dicen que no hay con quién hablar, no que la
# consulta estuviera mal.
_ASYNCPG_UNAVAILABLE = (
    # La conexión se cortó a mitad de una consulta.
    asyncpg.ConnectionDoesNotExistError,
    # SQLSTATE 08: no se pudo conectar o se perdió la conexión.
    asyncpg.PostgresConnectionError,
    # SQLSTATE 57P01 a 57P03: la base se apaga, se cayó o todavía arranca.
    asyncpg.exceptions.AdminShutdownError,
    asyncpg.exceptions.CrashShutdownError,
    asyncpg.CannotConnectNowError,
)
_DRIVER_MODULES = ("asyncpg", "sqlalchemy", "aiosqlite")

logger = get_logger("resthub.database")


def _causes(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        # SQLAlchemy envuelve el error del driver en `orig`; el adaptador de
        # asyncpg, a su vez, deja el de asyncpg como causa.
        original = current.orig if isinstance(current, DBAPIError) else None
        current = original or current.__cause__ or current.__context__
    return chain


def _raised_by_driver(error: BaseException) -> bool:
    """Un `OSError` cuenta solo si salió del driver de la base, no de otro lado."""
    return any(
        str(frame.f_globals.get("__name__", "")).startswith(_DRIVER_MODULES)
        for frame, _ in traceback.walk_tb(error.__traceback__)
    )


def is_database_unavailable(error: BaseException) -> bool:
    if isinstance(error, PoolTimeoutError):
        return True
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    for cause in _causes(error):
        if isinstance(cause, _ASYNCPG_UNAVAILABLE):
            return True
        # `ConnectionRefusedError`, `ConnectionResetError`, el `TimeoutError`
        # de conectar: todos son `OSError`.
        if isinstance(cause, OSError) and _raised_by_driver(cause):
            return True
    return False


async def _database_unavailable(_: Request, error: Exception) -> JSONResponse:
    if not is_database_unavailable(error):
        # Un error del programa: sigue su camino hasta el 500 con su traza.
        raise error
    logger.error("database.unavailable", error=type(error).__name__, reason=str(error)[:200])
    return JSONResponse(
        {"detail": DATABASE_UNAVAILABLE_DETAIL},
        status_code=503,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def install_database_error_handlers(app: FastAPI) -> None:
    for error_type in (DBAPIError, PoolTimeoutError, OSError):
        app.add_exception_handler(error_type, _database_unavailable)
