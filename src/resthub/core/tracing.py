"""Trazas distribuidas con OpenTelemetry, apagadas por omisión.

Con `OTEL_EXPORTER_OTLP_ENDPOINT` vacío no se importa ni se instala nada de OpenTelemetry:
el costo en tiempo de ejecución es cero. Con un endpoint (un colector OTLP/HTTP, como el que
ofrece Railway) se instrumentan FastAPI, SQLAlchemy asíncrono y httpx, y cada traza sale por
OTLP/HTTP con el muestreo que dice `OTEL_TRACES_SAMPLER_ARG`.

Es independiente de la telemetría propia de la plataforma (`core/telemetry.py`, el panel de
observabilidad): esa vive en la base y sigue igual. Lo que une a los dos mundos es el log:
mientras hay una traza activa, cada evento de structlog lleva `trace_id` y `span_id` (en
hexadecimal, como los muestra el colector), así que de un log se salta a su traza y al revés.

Nada de esto lee el entorno: el endpoint, el nombre del servicio y el muestreo llegan por
`Settings` (ruff `TID251`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from structlog.typing import EventDict, WrappedLogger

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from sqlalchemy.ext.asyncio import AsyncEngine

    from resthub.core.config import Settings

# Lo que se instrumenta sin trazar: el sondeo de vida (cada pocos segundos) y el canal de
# avisos (una conexión abierta por horas). Es una expresión regular sobre la URL.
EXCLUDED_URLS = r"/api/v1/health,/api/v1/events"
TRACES_PATH = "/v1/traces"

# `None` mientras no hay trazas: el procesador de logs no hace nada y no importa OpenTelemetry.
_current_ids: Callable[[], tuple[str, str] | None] | None = None


def add_trace_context(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Suma `trace_id` y `span_id` a cada evento mientras haya una traza activa."""
    if _current_ids is not None and (ids := _current_ids()) is not None:
        event_dict["trace_id"], event_dict["span_id"] = ids
    return event_dict


def traces_url(endpoint: str) -> str:
    """El endpoint de trazas: la base que da el colector más `/v1/traces` (OTLP/HTTP)."""
    base = endpoint.strip().rstrip("/")
    return base if base.endswith(TRACES_PATH) else f"{base}{TRACES_PATH}"


def parse_headers(raw: str | None) -> dict[str, str]:
    """`clave=valor,otra=valor` (el formato de `OTEL_EXPORTER_OTLP_HEADERS`) a diccionario."""
    pairs = (item.partition("=") for item in (raw or "").split(",") if item.strip())
    return {key.strip(): value.strip() for key, _, value in pairs if key.strip()}


@dataclass(slots=True)
class Tracing:
    """Lo instalado, con cómo deshacerlo: apagado ordenado y pruebas aisladas."""

    provider: TracerProvider
    undo: list[Callable[[], None]] = field(default_factory=list)

    def shutdown(self) -> None:
        global _current_ids  # noqa: PLW0603 - el procesador de logs lee este gancho
        _current_ids = None
        for undo in reversed(self.undo):
            undo()
        self.provider.shutdown()


def install_tracing(
    app: FastAPI,
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    span_processors: Sequence[SpanProcessor] | None = None,
) -> Tracing | None:
    """Instala las trazas si hay endpoint; si no, no hace nada y devuelve `None`.

    `span_processors` reemplaza al exportador OTLP: las pruebas pasan uno en memoria.
    """
    endpoint = (settings.otel_exporter_otlp_endpoint or "").strip()
    if not endpoint and span_processors is None:
        return None

    # Importaciones tardías a propósito: con las trazas apagadas, OpenTelemetry no se carga.
    from opentelemetry import trace  # noqa: PLC0415
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased  # noqa: PLC0415

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name or settings.app_name,
                "service.version": settings.app_version,
            }
        ),
        # Respeta la decisión de quien llamó (un frontend o un proxy con traza) y, si la
        # traza empieza aquí, deja pasar solo la fracción pedida.
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_arg)),
    )
    for processor in span_processors or [
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=traces_url(endpoint),
                headers=parse_headers(settings.otel_exporter_otlp_headers) or None,
            )
        )
    ]:
        provider.add_span_processor(processor)

    tracing = Tracing(provider=provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls=EXCLUDED_URLS)
    tracing.undo.append(lambda: FastAPIInstrumentor.uninstrument_app(app))
    httpx_instrumentor = HTTPXClientInstrumentor()
    httpx_instrumentor.instrument(tracer_provider=provider)
    tracing.undo.append(httpx_instrumentor.uninstrument)
    if engine is not None:
        sqlalchemy_instrumentor = SQLAlchemyInstrumentor()
        sqlalchemy_instrumentor.instrument(engine=engine.sync_engine, tracer_provider=provider)
        tracing.undo.append(sqlalchemy_instrumentor.uninstrument)

    def current_ids() -> tuple[str, str] | None:
        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None
        return format(context.trace_id, "032x"), format(context.span_id, "016x")

    global _current_ids  # noqa: PLW0603 - el procesador de logs lee este gancho
    _current_ids = current_ids
    return tracing
