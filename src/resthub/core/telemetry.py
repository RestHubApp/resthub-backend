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
- Si escribir un lote falla, se reintenta una vez y se descarta.
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
import traceback
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from structlog.typing import EventDict, WrappedLogger

from resthub.core.config import get_settings
from resthub.core.redaction import redact
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
MAX_EVENT_LENGTH = 255
MAX_LOGGER_LENGTH = 120
MAX_FIELD_TEXT_LENGTH = 2_000
TRUNCATED_MARK = "…[recortado]\n"

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
# Claves del evento que ya tienen su columna o que son del mecanismo de logs.
_RESERVED_KEYS = frozenset(
    {"event", "level", "logger", "timestamp", "request_id", "exc_info", "stack_info"}
)

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
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._enabled = enabled
        self._retention = timedelta(days=retention_days)
        self._capacity = capacity
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._retry_delay = retry_delay
        self._purge_interval = purge_interval
        self._clock = clock
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
        with self._guard:
            if len(self._pending) >= self._capacity:
                self._pending.popleft()
                self._dropped += 1
            self._pending.append(item)
            full = len(self._pending) >= self._batch_size
        if full:
            self._signal()

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
        requests = [item for item in batch if isinstance(item, RequestRecord)]
        events = [item for item in batch if isinstance(item, EventRecord)]
        with suppressed_capture():
            for attempt in (1, 2):
                try:
                    await sink.write(requests, events)
                    return
                except Exception as error:
                    if attempt == 1:
                        await asyncio.sleep(self._retry_delay)
                        continue
                    with self._guard:
                        self._dropped += len(batch)
                    _writer_logger.warning(
                        "telemetry.write_failed", rows=len(batch), error=type(error).__name__
                    )

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
        )
    return _Holder.recorder


def set_telemetry(recorder: TelemetryRecorder | None) -> None:
    """Reemplaza el registro del proceso; `None` vuelve a armarlo con la configuración."""
    _Holder.recorder = recorder


# --- Eventos de log -----------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json_safe(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _truncate(value, MAX_FIELD_TEXT_LENGTH)
    if depth < 6 and isinstance(value, Mapping):
        return {str(key): _json_safe(item, depth + 1) for key, item in value.items()}
    if depth < 6 and isinstance(value, list | tuple):
        return [_json_safe(item, depth + 1) for item in value]
    return _truncate(str(value), MAX_FIELD_TEXT_LENGTH)


def _exception_of(exc_info: Any) -> BaseException | None:
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        return exc_info[1]
    if exc_info:
        return sys.exc_info()[1]
    return None


def format_traceback(error: BaseException) -> str:
    """El traceback completo, o su final si es muy largo: ahí están el tipo y el mensaje."""
    text = "".join(traceback.format_exception(error))
    if len(text) <= MAX_TRACEBACK_LENGTH:
        return text
    return TRUNCATED_MARK + text[-(MAX_TRACEBACK_LENGTH - len(TRUNCATED_MARK)) :]


def _event_record(method_name: str, event_dict: EventDict) -> EventRecord | None:
    level = _LEVELS.get(str(event_dict.get("level", method_name)).lower())
    if level is None:
        return None
    context = current_request_context()
    if context is not None and not context.captured:
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
        raw_fields["error_message"] = str(error)
        trace = format_traceback(error)
    fields = _json_safe(redact(raw_fields))

    restaurant_id = event_dict.get("restaurant_id")
    if not isinstance(restaurant_id, int) or isinstance(restaurant_id, bool):
        restaurant_id = context.restaurant_id if context is not None else None
    request_id = event_dict.get("request_id")
    return EventRecord(
        at=_utcnow(),
        level=level,
        logger=_truncate(str(event_dict.get("logger", "")), MAX_LOGGER_LENGTH),
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
