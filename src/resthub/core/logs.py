"""Configuración de los logs.

Todo pasa por structlog, también lo que escriben uvicorn, SQLAlchemy o
cualquier librería que use `logging`: sin eso, cada una saldría con su propio
formato y un recolector no podría filtrar por campo. Cada evento lleva los
datos de la petición en curso (`request_id`, método y ruta) porque el
middleware los guarda en variables de contexto y `merge_contextvars` los suma.

El nombre del módulo no es `logging` para no tapar a la librería estándar.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

# Claves que nunca se escriben en claro, vengan de un evento propio o de una
# librería. Se comparan en minúsculas.
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "jwt_secret_key",
        "new_password",
        "password",
        "token",
        "typesafe_api_key",
    }
)
REDACTED = "[oculto]"

# Marca los handlers que agrega esta configuración, para reemplazarlos al
# volver a configurar sin tocar los que agregó otro, como la captura de pytest.
_OWN_HANDLER_ATTRIBUTE = "_resthub_handler"


def redact_sensitive(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Reemplaza el valor de las claves sensibles antes de renderizar."""
    for key in event_dict:
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def drop_color_message(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Quita la copia con colores ANSI que uvicorn agrega a sus propios mensajes."""
    event_dict.pop("color_message", None)
    return event_dict


def mask_email(email: str) -> str:
    """Deja reconocible un correo sin escribirlo entero: `j***@example.com`.

    Alcanza para ver que los intentos fallidos se repiten contra la misma
    cuenta sin que el log se vuelva un listado de correos del personal.
    """
    local, separator, domain = email.partition("@")
    if not separator:
        return REDACTED
    return f"{local[:1]}***@{domain}"


def configure_logging(*, level: str, json: bool) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        drop_color_message,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_sensitive,
    ]
    render: list[Processor] = (
        [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
        if json
        else [structlog.dev.ConsoleRenderer()]
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *render],
        )
    )
    setattr(handler, _OWN_HANDLER_ATTRIBUTE, True)

    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _OWN_HANDLER_ATTRIBUTE, False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn instala sus propios handlers al arrancar. Se quitan para que sus
    # líneas bajen a la raíz y salgan con el mismo formato.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # El log de acceso de uvicorn se apaga: el middleware registra cada
    # petición con más datos (duración, identificador), y tenerlos dos veces
    # solo duplica el volumen.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    # Con INFO, SQLAlchemy escribiría cada consulta.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
