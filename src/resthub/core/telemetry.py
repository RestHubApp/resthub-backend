"""Telemetría propia: qué pasó en cada petición y qué avisos dejó la aplicación.

Es la captura del panel de observabilidad de la plataforma. Vive en el núcleo
porque atraviesa toda la aplicación: el middleware de registro anota cada
petición (`core/request_logging.py`) y un procesador de structlog anota cada
evento de nivel `warning` o superior, con el traceback si trae una excepción.
Dónde se guarda no es cosa del núcleo: lo decide el sumidero (`TelemetrySink`)
que la raíz de composición instala al arrancar; el de la base de datos es del
módulo `platform`, que también es el que lo consulta.

Capturar nunca frena ni rompe una petición:

- La captura solo encola en memoria. Un bucle de fondo escribe en lotes, cada
  `FLUSH_INTERVAL_SECONDS` o en cuanto se juntan `BATCH_SIZE` filas.
- La cola tiene tope. Si se llena (la base no responde, una avalancha), se
  descarta lo más viejo y se cuenta: el panel muestra cuánto se perdió.
- Cada proceso encola a lo sumo `MAX_ROWS_PER_MINUTE` filas por minuto
  (configurable); lo que pasa se descarta y se cuenta igual. De las peticiones
  que no llegaron a ninguna ruta se guarda solo una muestra.
- Si escribir un lote falla, se reintenta una vez; si vuelve a fallar, se
  prueba fila por fila y se descartan (y cuentan) solo las que la base rechace.
- Cada valor se recorta al largo de su columna antes de encolarse y el método
  HTTP se reduce a los conocidos (`OTHER` para el resto): una fila que no
  entra no puede tumbar el lote de las demás.
- Lo que escribe el propio bucle (sus consultas, sus avisos) no se captura: un
  aviso por no poder escribir generaría otra fila que tampoco se podría escribir.

Con varios procesos cada uno tiene su cola y escribe lo suyo; el conteo de
descartes es del proceso que responde.

Nunca se guardan cuerpos, cabeceras ni query strings: la petición se anota
con la plantilla de su ruta, y los campos de los eventos pasan por
`core/redaction.py` antes de encolarse.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from structlog.typing import EventDict, WrappedLogger

from resthub.core.config import get_settings
from resthub.core.redaction import redact, scrub_text
from resthub.core.request_context import current_request_context

# Tope de la cola en memoria, sumando peticiones y eventos.
QUEUE_CAPACITY = 10_000
BATCH_SIZE = 200
FLUSH_INTERVAL_SECONDS = 2.0
# Espera antes del único reintento de un lote que no se pudo escribir.
RETRY_DELAY_SECONDS = 0.5
PURGE_INTERVAL_SECONDS = 3600.0
# Lo que se da al apagar para escribir lo que quedó en la cola.
SHUTDOWN_FLUSH_SECONDS = 5.0

MAX_TRACEBACK_LENGTH = 20_000
MAX_FIELD_TEXT_LENGTH = 2_000
TRUNCATED_MARK = "…[recortado]\n"
# Largo de cada columna de texto de `obs_requests` y `obs_events` (los modelos
# de `platform` los usan). Todo se recorta antes de encolar: un valor que no
# entra haría que PostgreSQL rechace el lote entero, y el que lo manda (un
# método HTTP inventado, un campo enorme) podría dejar ciego al panel.
MAX_METHOD_LENGTH = 10
MAX_ROUTE_LENGTH = 255
MAX_REQUEST_ID_LENGTH = 128
MAX_ACCOUNT_KIND_LENGTH = 16
MAX_LEVEL_LENGTH = 10
MAX_LOGGER_LENGTH = 120
MAX_EVENT_LENGTH = 255
# Las columnas enteras son de 32 bits en PostgreSQL.
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1

# El método se guarda solo si es uno de estos; cualquier otro (h11 acepta
# cualquier palabra, como `UNSUBSCRIBE`) se anota como `OTHER`.
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
OTHER_METHOD = "OTHER"
# Después de dos fallas de un lote se prueba fila por fila, para perder solo
# las malas. Si fallan tantas seguidas es la base la que no responde, no una
# fila: se deja de insistir y se descarta el resto.
MAX_CONSECUTIVE_ROW_FAILURES = 5
# Tope de filas por minuto que encola cada proceso (peticiones más eventos),
# con un balde de fichas: se permite una ráfaga de hasta un minuto de filas y
# lo que pasa del ritmo se descarta y se cuenta. Una avalancha (un bot, un
# error en bucle) no llena la base. 0 lo quita.
MAX_ROWS_PER_MINUTE = 6_000
# Lo que se guarda cuando la petición no llegó a ninguna ruta (un 404 de una
# dirección inventada, una consulta previa de CORS). Agruparlas evita una fila
# por cada dirección que invente un bot, y de ellas se guarda solo una de cada
# `UNMATCHED_SAMPLE_EVERY`: es el tráfico que cualquiera genera sin límite.
# Las descartadas por la muestra no cuentan como perdidas.
UNMATCHED_ROUTE = "<sin ruta>"
UNMATCHED_SAMPLE_EVERY = 10

WARNING = "warning"
ERROR = "error"
# Lo que no es un aviso cuenta como error: el panel filtra por esos dos.
_LEVELS = {
    "warn": WARNING,
    "warning": WARNING,
    "error": ERROR,
    "exception": ERROR,
    "critical": ERROR,
    "fatal": ERROR,
}
# Claves del evento que ya tienen su columna, que son del mecanismo de logs o
# que ya están en la fila de la petición (`method` y `path` los suma
# `merge_contextvars` a cada evento; `path` es la ruta cruda, con ids, y la
# petición ya guarda su plantilla).
_RESERVED_KEYS = frozenset(
    {
        "event",
        "level",
        "logger",
        "timestamp",
        "request_id",
        "restaurant_id",
        "method",
        "path",
        "exc_info",
        "stack_info",
    }
)
# Loggers del servidor. Cuando una petición falla, uvicorn vuelve a loguear la
# misma excepción ("Exception in ASGI application") con el mismo `request_id`:
# si el middleware ya guardó su `request.failed`, esa copia se ignora.
_SERVER_LOGGER = "uvicorn"

# El bucle de escritura y la purga marcan su contexto para que lo que logueen
# (ellos o SQLAlchemy por debajo) no vuelva a la cola.
_suppressed: ContextVar[bool] = ContextVar("resthub_telemetry_suppressed", default=False)

_writer_logger = structlog.stdlib.get_logger("resthub.telemetry")


@dataclass(frozen=True, slots=True)
class RequestRecord:
    at: datetime
    method: str
    # La plantilla (`/api/v1/orders/{order_id}`), nunca la ruta con ids.
    route: str
    status: int
    duration_ms: float
    db_ms: float
    db_queries: int
    request_id: str
    account_kind: str
    restaurant_id: int | None = None
    account_id: int | None = None


@dataclass(frozen=True, slots=True)
class EventRecord:
    at: datetime
    level: str
    logger: str
    event: str
    request_id: str | None = None
    restaurant_id: int | None = None
    # Ya sin datos sensibles y listos para JSON.
    fields: dict[str, Any] = field(default_factory=dict)
    traceback: str | None = None


class TelemetrySink(Protocol):
    """Dónde terminan las filas. Lo instala la raíz de composición."""

    async def write(
        self, requests: Sequence[RequestRecord], events: Sequence[EventRecord]
    ) -> None: ...

    async def purge(self, before: datetime) -> int:
        """Borra lo anterior a `before` y dice cuántas filas borró."""
        ...


@contextmanager
def suppressed_capture() -> Iterator[None]:
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_method(method: object) -> str:
    """El método HTTP si es uno conocido; si no, `OTHER`."""
    text = str(method).upper()
    return text if text in HTTP_METHODS else OTHER_METHOD


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_text(value: object, limit: int | None = None) -> str:
    """Texto que PostgreSQL acepta: sin `NUL`, sin sustitutos sueltos y recortado."""
    text = value if isinstance(value, str) else str(value)
    if "\x00" in text:
        text = text.replace("\x00", "")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8")
    return text if limit is None else _truncate(text, limit)


def _optional_text(value: str | None, limit: int) -> str | None:
    return None if value is None else clean_text(value, limit)


def _int32(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if _INT32_MIN <= value <= _INT32_MAX else None


def _bounded(item: RequestRecord | EventRecord) -> RequestRecord | EventRecord:
    """La fila con cada valor dentro de su columna."""
    if isinstance(item, RequestRecord):
        return replace(
            item,
            method=normalize_method(item.method),
            route=clean_text(item.route, MAX_ROUTE_LENGTH),
            status=_int32(item.status) or 0,
            db_queries=_int32(item.db_queries) or 0,
            request_id=clean_text(item.request_id, MAX_REQUEST_ID_LENGTH),
            account_kind=clean_text(item.account_kind, MAX_ACCOUNT_KIND_LENGTH),
            restaurant_id=_int32(item.restaurant_id),
            account_id=_int32(item.account_id),
        )
    return replace(
        item,
        level=clean_text(item.level, MAX_LEVEL_LENGTH),
        logger=clean_text(item.logger, MAX_LOGGER_LENGTH),
        event=clean_text(item.event, MAX_EVENT_LENGTH),
        request_id=_optional_text(item.request_id, MAX_REQUEST_ID_LENGTH),
        restaurant_id=_int32(item.restaurant_id),
        traceback=_optional_text(item.traceback, MAX_TRACEBACK_LENGTH),
    )


def _split(
    batch: Sequence[RequestRecord | EventRecord],
) -> tuple[list[RequestRecord], list[EventRecord]]:
    requests = [item for item in batch if isinstance(item, RequestRecord)]
    events = [item for item in batch if isinstance(item, EventRecord)]
    return requests, events


class TelemetryRecorder:
    def __init__(
        self,
        *,
        enabled: bool = True,
        retention_days: int = 14,
        capacity: int = QUEUE_CAPACITY,
        batch_size: int = BATCH_SIZE,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        retry_delay: float = RETRY_DELAY_SECONDS,
        purge_interval: float = PURGE_INTERVAL_SECONDS,
        max_rows_per_minute: int = MAX_ROWS_PER_MINUTE,
        unmatched_sample_every: int = UNMATCHED_SAMPLE_EVERY,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._retention = timedelta(days=retention_days)
        self._capacity = capacity
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._retry_delay = retry_delay
        self._purge_interval = purge_interval
        self._clock = clock
        self._monotonic = monotonic
        self._rows_per_minute = max(0, max_rows_per_minute)
        self._tokens = float(self._rows_per_minute)
        self._refilled_at = monotonic()
        self._unmatched_every = max(1, unmatched_sample_every)
        self._unmatched_seen = 0
        self._sink: TelemetrySink | None = None
        # Una `deque` y no una `asyncio.Queue`: la captura también llega desde
        # hilos (dependencias síncronas, librerías que loguean con `logging`),
        # y la cola de asyncio no se puede tocar desde otro hilo.
        self._pending: deque[RequestRecord | EventRecord] = deque()
        self._guard = threading.Lock()
        self._dropped = 0
        self._write_lock: asyncio.Lock | None = None
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active(self) -> bool:
        """Si se está capturando: encendida y con un sumidero instalado."""
        return self._enabled and self._sink is not None

    @property
    def dropped(self) -> int:
        """Filas perdidas en este proceso: por cola llena o por no poder escribirlas."""
        return self._dropped

    @property
    def pending(self) -> int:
        return len(self._pending)

    def install(self, sink: TelemetrySink) -> None:
        self._sink = sink

    # --- Captura -------------------------------------------------------------

    def record(self, item: RequestRecord | EventRecord) -> None:
        """Encola sin esperar. Seguro desde cualquier hilo; nunca lanza."""
        if not self.active or _suppressed.get():
            return
        try:
            item = _bounded(item)
        except Exception:
            return
        with self._guard:
            if not self._sampled_in(item):
                return
            if not self._take_token():
                self._dropped += 1
                return
            if len(self._pending) >= self._capacity:
                self._pending.popleft()
                self._dropped += 1
            self._pending.append(item)
            full = len(self._pending) >= self._batch_size
        if full:
            self._signal()

    def _sampled_in(self, item: RequestRecord | EventRecord) -> bool:
        """Si la fila entra en la muestra. Se llama con `_guard` tomado."""
        if not isinstance(item, RequestRecord) or item.route != UNMATCHED_ROUTE:
            return True
        seen, self._unmatched_seen = self._unmatched_seen, self._unmatched_seen + 1
        return seen % self._unmatched_every == 0

    def _take_token(self) -> bool:
        """Una ficha del balde de filas por minuto. Se llama con `_guard` tomado."""
        if not self._rows_per_minute:
            return True
        now = self._monotonic()
        refill = (now - self._refilled_at) * self._rows_per_minute / 60
        self._tokens = min(float(self._rows_per_minute), self._tokens + refill)
        self._refilled_at = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def _signal(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            wake.set()
        else:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(wake.set)

    # --- Escritura -----------------------------------------------------------

    def _take_batch(self) -> list[RequestRecord | EventRecord]:
        with self._guard:
            size = min(self._batch_size, len(self._pending))
            return [self._pending.popleft() for _ in range(size)]

    def _lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    async def flush(self) -> None:
        """Escribe todo lo encolado. Las pruebas lo llaman para no esperar al bucle."""
        async with self._lock():
            while batch := self._take_batch():
                await self._write(batch)

    async def _write(self, batch: list[RequestRecord | EventRecord]) -> None:
        sink = self._sink
        if sink is None:
            return
        with suppressed_capture():
            error: Exception | None = None
            for attempt in (1, 2):
                try:
                    await sink.write(*_split(batch))
                    return
                except Exception as failure:
                    error = failure
                    if attempt == 1:
                        await asyncio.sleep(self._retry_delay)
            lost = await self._write_one_by_one(sink, batch) if len(batch) > 1 else len(batch)
            if lost:
                self._count_dropped(lost)
                _writer_logger.warning(
                    "telemetry.write_failed",
                    rows=lost,
                    batch=len(batch),
                    error=type(error).__name__,
                )

    async def _write_one_by_one(
        self, sink: TelemetrySink, batch: list[RequestRecord | EventRecord]
    ) -> int:
        """Escribe fila por fila y dice cuántas se perdieron."""
        lost = consecutive = 0
        for index, item in enumerate(batch):
            try:
                await sink.write(*_split([item]))
            except Exception:
                lost += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_ROW_FAILURES:
                    return lost + len(batch) - index - 1
            else:
                consecutive = 0
        return lost

    def _count_dropped(self, rows: int) -> None:
        with self._guard:
            self._dropped += rows

    async def purge(self, now: datetime | None = None) -> int:
        """Borra lo que pasó la retención."""
        sink = self._sink
        if sink is None:
            return 0
        cutoff = (now or self._clock()) - self._retention
        with suppressed_capture():
            return await sink.purge(cutoff)

    # --- Ciclo de vida -------------------------------------------------------

    async def start(self) -> None:
        """Arranca el bucle de escritura y la purga. Sin sumidero no hace nada."""
        if not self.active or self._tasks:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._tasks = [
            self._loop.create_task(self._writer_loop(), name="telemetry-writer"),
            self._loop.create_task(self._purge_loop(), name="telemetry-purge"),
        ]

    async def stop(self) -> None:
        """Corta los bucles y escribe lo que quedó, con un plazo."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop = None
        self._wake = None
        if self.active:
            with suppress(TimeoutError):
                await asyncio.wait_for(self.flush(), timeout=SHUTDOWN_FLUSH_SECONDS)

    async def _writer_loop(self) -> None:
        _suppressed.set(True)
        wake = self._wake
        assert wake is not None
        while True:
            with suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=self._flush_interval)
            wake.clear()
            try:
                await self.flush()
            except Exception as error:
                # `_write` ya contiene los errores del sumidero; esto es la red
                # de seguridad para que el bucle no muera.
                _writer_logger.warning("telemetry.flush_failed", error=type(error).__name__)

    async def _purge_loop(self) -> None:
        _suppressed.set(True)
        while True:
            try:
                removed = await self.purge()
                if removed:
                    _writer_logger.info("telemetry.purged", rows=removed)
            except Exception as error:
                _writer_logger.warning("telemetry.purge_failed", error=type(error).__name__)
            await asyncio.sleep(self._purge_interval)


class _Holder:
    recorder: TelemetryRecorder | None = None


def get_telemetry() -> TelemetryRecorder:
    """El registro del proceso. Se arma con la configuración la primera vez."""
    if _Holder.recorder is None:
        settings = get_settings()
        _Holder.recorder = TelemetryRecorder(
            enabled=settings.observability_enabled,
            retention_days=settings.observability_retention_days,
            max_rows_per_minute=settings.observability_max_rows_per_minute,
        )
    return _Holder.recorder


def set_telemetry(recorder: TelemetryRecorder | None) -> None:
    """Reemplaza el registro del proceso; `None` vuelve a armarlo con la configuración."""
    _Holder.recorder = recorder


# --- Eventos de log -----------------------------------------------------------


def _json_safe(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return clean_text(value, MAX_FIELD_TEXT_LENGTH)
    if depth < 6 and isinstance(value, Mapping):
        return {
            clean_text(key, MAX_EVENT_LENGTH): _json_safe(item, depth + 1)
            for key, item in value.items()
        }
    if depth < 6 and isinstance(value, list | tuple):
        return [_json_safe(item, depth + 1) for item in value]
    return clean_text(value, MAX_FIELD_TEXT_LENGTH)


def _exception_of(exc_info: Any) -> BaseException | None:
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        return exc_info[1]
    if exc_info:
        return sys.exc_info()[1]
    return None


def format_traceback(error: BaseException) -> str:
    """El traceback completo, o su final si es muy largo: ahí están el tipo y el mensaje.

    Se limpia antes de recortar: recortar primero podría dejar los valores de
    una consulta sin el `[parameters: ` que los delata.
    """
    text = scrub_text("".join(traceback.format_exception(error)))
    if len(text) <= MAX_TRACEBACK_LENGTH:
        return text
    return TRUNCATED_MARK + text[-(MAX_TRACEBACK_LENGTH - len(TRUNCATED_MARK)) :]


def _is_server_logger(name: str) -> bool:
    return name == _SERVER_LOGGER or name.startswith(_SERVER_LOGGER + ".")


def _event_record(method_name: str, event_dict: EventDict) -> EventRecord | None:
    level = _LEVELS.get(str(event_dict.get("level", method_name)).lower())
    if level is None:
        return None
    context = current_request_context()
    if context is not None and not context.captured:
        return None
    logger_name = str(event_dict.get("logger", ""))
    if context is not None and context.failure_captured and _is_server_logger(logger_name):
        return None

    raw_fields = {
        key: value
        for key, value in event_dict.items()
        if key not in _RESERVED_KEYS and not str(key).startswith("_")
    }
    error = _exception_of(event_dict.get("exc_info"))
    trace = None
    if error is not None:
        raw_fields["error_type"] = type(error).__name__
        raw_fields["error_message"] = scrub_text(str(error))
        trace = format_traceback(error)
    fields = _json_safe(redact(raw_fields))

    restaurant_id = event_dict.get("restaurant_id")
    if not isinstance(restaurant_id, int) or isinstance(restaurant_id, bool):
        restaurant_id = context.restaurant_id if context is not None else None
    request_id = event_dict.get("request_id")
    return EventRecord(
        at=_utcnow(),
        level=level,
        logger=_truncate(logger_name, MAX_LOGGER_LENGTH),
        event=_truncate(str(event_dict.get("event", "")), MAX_EVENT_LENGTH),
        request_id=request_id if isinstance(request_id, str) else None,
        restaurant_id=restaurant_id,
        fields=fields,
        traceback=trace,
    )


def capture_log_event(_: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Procesador de structlog: encola los avisos y errores. Deja el evento intacto."""
    recorder = get_telemetry()
    if not recorder.active or _suppressed.get():
        return event_dict
    # Capturar nunca puede romper el log que la dispara.
    with suppress(Exception):
        record = _event_record(method_name, event_dict)
        if record is not None:
            recorder.record(record)
    return event_dict
