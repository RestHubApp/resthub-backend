"""Configuración de los logs.

Todo pasa por structlog, también lo que escriben uvicorn, SQLAlchemy o
cualquier librería que use `logging`: sin eso, cada una saldría con su propio
formato y un recolector no podría filtrar por campo. Cada evento lleva los
datos de la petición en curso (`request_id`, método y ruta) porque el
middleware los guarda en variables de contexto y `merge_contextvars` los suma.

Antes de escribirse, cada línea pasa por la misma limpieza que los eventos del
panel de observabilidad (`core/redaction.py`): las claves sensibles o con datos
personales se ocultan y todo texto (el evento, los valores, el mensaje y el
traceback de una excepción) se limpia de credenciales, correos y números de
documento o teléfono. Los tracebacks de la consola no llevan las variables
locales de cada marco: podrían traer una contraseña o un dato personal.

El nombre del módulo no es `logging` para no tapar a la librería estándar.
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import TextIO

import structlog
from structlog.typing import EventDict, ExcInfo, Processor, WrappedLogger

from resthub.core.redaction import REDACTED, is_sensitive_key, redact, scrub_text
from resthub.core.telemetry import capture_log_event
from resthub.core.tracing import add_trace_context

# Lo que structlog o `logging` dejan en el evento para su propio uso: la
# excepción viva (se limpia ya renderizada) y el registro original.
_UNREDACTED_KEYS = frozenset({"exc_info", "_record", "_from_structlog"})
# `REDACTED` se reexporta desde `core/redaction.py`, que tiene la regla más
# amplia con la que se guardan los eventos del panel de observabilidad.
__all__ = [
    "REDACTED",
    "configure_logging",
    "get_logger",
    "mask_email",
    "redact_exception",
    "redact_sensitive",
]

# Marca los handlers que agrega esta configuración, para reemplazarlos al
# volver a configurar sin tocar los que agregó otro, como la captura de pytest.
_OWN_HANDLER_ATTRIBUTE = "_resthub_handler"


def redact_sensitive(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Oculta las claves sensibles y limpia los textos antes de renderizar.

    Corre una sola vez por evento, con la regla de `core/redaction.py`. Es
    idempotente: lo ya oculto no cambia si vuelve a pasar (como hace la captura
    del panel, que lo limpia otra vez por su cuenta).
    """
    for key, value in event_dict.items():
        if key in _UNREDACTED_KEYS:
            continue
        event_dict[key] = REDACTED if is_sensitive_key(key) else redact(value)
    return event_dict


def redact_exception(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Limpia la excepción ya convertida en texto o en diccionario."""
    exception = event_dict.get("exception")
    if isinstance(exception, str):
        event_dict["exception"] = scrub_text(exception)
    elif exception is not None:
        event_dict["exception"] = redact(exception)
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


def scrubbed_traceback(sio: TextIO, exc_info: ExcInfo) -> None:
    """El traceback de la consola de desarrollo, limpio y sin variables locales."""
    sio.write("\n" + scrub_text("".join(traceback.format_exception(*exc_info))))


def configure_logging(*, level: str, json: bool) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        drop_color_message,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Con trazas activas, el log lleva el `trace_id` de la petición que lo escribió.
        add_trace_context,
        redact_sensitive,
        # Al final, con el evento ya completo: encola los avisos y errores para
        # el panel de observabilidad (y los oculta con su propia regla).
        capture_log_event,
    ]
    # La excepción se convierte primero (a diccionario o a texto) para poder
    # limpiarla antes de escribirla.
    render: list[Processor] = (
        [
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            redact_exception,
            structlog.processors.JSONRenderer(),
        ]
        if json
        else [structlog.dev.ConsoleRenderer(exception_formatter=scrubbed_traceback)]
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
    # Con INFO, httpx escribiría la URL de cada llamada a un tercero, y la de la
    # consulta de RUC/DNI lleva el número de documento: un dato personal.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
