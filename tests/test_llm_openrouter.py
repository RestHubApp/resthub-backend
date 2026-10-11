"""El adaptador de OpenRouter, contra un transporte HTTP falso."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from resthub.core.llm import JsonCompletionRequest, LlmUnavailableError
from resthub.core.llm_openrouter import OpenRouterLlmClient
from tests.conftest import url

REQUEST = JsonCompletionRequest(
    system="sistema", user="contexto", schema_name="prueba", schema={"type": "object"}
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OpenRouterLlmClient:
    return OpenRouterLlmClient(
        api_key="clave-de-prueba",
        base_url="https://openrouter.test/api/v1",
        models=("modelo/principal", "modelo/respaldo"),
        referer="http://localhost:5173",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


async def test_requests_json_without_reasoning_and_without_data_retention() -> None:
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        content = '```json\n{"resumen": "Todo en orden."}\n```'
        return httpx.Response(
            200,
            json={
                "model": "modelo/respaldo",
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.00003},
            },
        )

    result = await _client(respond).complete_json(REQUEST)

    body = json.loads(sent[0].content)
    assert sent[0].url.path == url("chat/completions")
    assert sent[0].headers["Authorization"] == "Bearer clave-de-prueba"
    assert body["models"] == ["modelo/principal", "modelo/respaldo"]
    assert body["reasoning"] == {"enabled": False}
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert result.data == {"resumen": "Todo en orden."}
    assert result.model == "modelo/respaldo"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (429, '{"error": "rate limit"}'),
        (200, '{"choices": []}'),
        (200, "no es json"),
        (200, '{"choices": [{"message": {"content": "[1, 2]"}}]}'),
    ],
)
async def test_provider_failure_leaves_the_assistant_unavailable(status: int, body: str) -> None:
    with pytest.raises(LlmUnavailableError):
        await _client(lambda _: httpx.Response(status, text=body)).complete_json(REQUEST)
