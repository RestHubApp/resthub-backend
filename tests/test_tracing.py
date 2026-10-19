"""Trazas OpenTelemetry: apagadas por omisión, y con un colector, un span por petición."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from opentelemetry.instrumentation.httpx import RequestInfo
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from resthub.core.config import Settings
from resthub.core.tracing import (
    Tracing,
    _hide_url_query,
    _hide_url_query_async,
    add_trace_context,
    install_tracing,
    parse_headers,
    traces_url,
)
from tests.conftest import StaffedRestaurant, authorization_for, url

SERVICE = "resthub-test"


@pytest.fixture
def tracing(app: FastAPI, session: AsyncSession, spans: InMemorySpanExporter) -> Iterator[Tracing]:
    """Instala las trazas sobre la aplicación de prueba con un exportador en memoria."""
    assert isinstance(session.bind, AsyncEngine)
    installed = install_tracing(
        app,
        Settings(otel_service_name=SERVICE),
        engine=session.bind,
        span_processors=[SimpleSpanProcessor(spans)],
    )
    assert installed is not None
    yield installed
    installed.shutdown()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def test_without_an_endpoint_nothing_is_installed(app: FastAPI) -> None:
    assert install_tracing(app, Settings(otel_exporter_otlp_endpoint=None)) is None
    assert install_tracing(app, Settings(otel_exporter_otlp_endpoint="  ")) is None
    assert app.state.tracing is None


def test_importing_the_app_does_not_load_opentelemetry() -> None:
    # FastAPI importa la API de OpenTelemetry (solo interfaces, sin costo); lo que
    # no debe cargarse sin colector es el SDK, los exportadores ni la instrumentación.
    code = (
        "import sys; import resthub.main; "
        "sys.exit(any(name.startswith(('opentelemetry.sdk', 'opentelemetry.exporter', "
        "'opentelemetry.instrumentation')) for name in sys.modules))"
    )

    result = subprocess.run(  # noqa: S603 - ejecuta el intérprete de la propia prueba
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


async def test_a_request_produces_a_server_span_with_the_route(
    client: AsyncClient,
    tracing: Tracing,
    spans: InMemorySpanExporter,
    restaurant_a: StaffedRestaurant,
) -> None:
    response = await client.get(url("orders/999999"), headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 404
    finished = spans.get_finished_spans()
    server = [span for span in finished if span.kind == SpanKind.SERVER]
    assert len(server) == 1
    attributes = server[0].attributes or {}
    assert attributes["http.route"] == url("orders/{order_id}")
    assert attributes["http.status_code"] == 404
    assert server[0].resource.attributes["service.name"] == SERVICE
    # Alguna consulta a la base cuelga de la misma traza (las de antes, de la siembra, no).
    database = [
        span
        for span in finished
        if span.kind == SpanKind.CLIENT and "db.system" in (span.attributes or {})
    ]
    assert server[0].context.trace_id in {span.context.trace_id for span in database}


async def test_liveness_and_events_are_not_traced(
    client: AsyncClient, tracing: Tracing, spans: InMemorySpanExporter
) -> None:
    await client.get(url("health"))

    assert [s for s in spans.get_finished_spans() if s.kind == SpanKind.SERVER] == []


def test_logs_carry_the_trace_id_only_inside_a_span(tracing: Tracing) -> None:
    outside = add_trace_context(None, "info", {"event": "fuera"})
    with tracing.provider.get_tracer("prueba").start_as_current_span("trabajo") as span:
        inside = add_trace_context(None, "info", {"event": "dentro"})
        context = span.get_span_context()

    assert "trace_id" not in outside
    assert inside["trace_id"] == format(context.trace_id, "032x")
    assert inside["span_id"] == format(context.span_id, "016x")


def test_logs_stay_untouched_when_tracing_is_off() -> None:
    assert add_trace_context(None, "info", {"event": "x"}) == {"event": "x"}


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://collector:4318", "http://collector:4318/v1/traces"),
        ("http://collector:4318/", "http://collector:4318/v1/traces"),
        ("https://otlp.example.com/v1/traces", "https://otlp.example.com/v1/traces"),
    ],
)
def test_traces_url_appends_the_otlp_path_once(endpoint: str, expected: str) -> None:
    assert traces_url(endpoint) == expected


def test_headers_are_parsed_like_the_otel_environment_variable() -> None:
    assert parse_headers("authorization=Bearer abc, x-team = rest") == {
        "authorization": "Bearer abc",
        "x-team": "rest",
    }
    assert parse_headers(None) == {}
    assert parse_headers(" , =sin-clave") == {}


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_outgoing_calls_drop_the_query_with_its_token(
    tracing: Tracing, spans: InMemorySpanExporter, asynchronous: bool
) -> None:
    """Lycet pide el token en la consulta (`?token=`): la traza guarda la URL sin ella."""
    request = RequestInfo(
        method=b"POST",
        url=httpx.URL("https://lycet.test/api/v1/invoice/send?token=secreto#x"),
        headers=None,
        stream=None,
        extensions=None,
    )
    span = tracing.provider.get_tracer("prueba").start_span("POST")
    span.set_attribute("url.full", str(request.url))

    if asynchronous:
        await _hide_url_query_async(span, request)
    else:
        _hide_url_query(span, request)
    span.end()

    (finished,) = spans.get_finished_spans()
    attributes = dict(finished.attributes or {})
    assert (
        attributes["url.full"]
        == attributes["http.url"]
        == ("https://lycet.test/api/v1/invoice/send")
    )
    assert "secreto" not in str(attributes)
