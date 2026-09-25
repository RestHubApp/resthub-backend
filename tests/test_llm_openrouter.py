"""El adaptador de OpenRouter, contra un transporte HTTP falso."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from resthub.core.llm import JsonCompletionRequest, LlmUnavailable
from resthub.core.llm_openrouter import OpenRouterLlmClient

PEDIDO = JsonCompletionRequest(
    system="sistema", user="contexto", schema_name="prueba", schema={"type": "object"}
)


def _cliente(handler: Callable[[httpx.Request], httpx.Response]) -> OpenRouterLlmClient:
    return OpenRouterLlmClient(
        api_key="clave-de-prueba",
        base_url="https://openrouter.test/api/v1",
        models=("modelo/principal", "modelo/respaldo"),
        referer="http://localhost:5173",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


async def test_pide_json_sin_razonamiento_y_sin_que_guarden_los_datos() -> None:
    enviados: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        enviados.append(request)
        contenido = '```json\n{"resumen": "Todo en orden."}\n```'
        return httpx.Response(
            200,
            json={
                "model": "modelo/respaldo",
                "choices": [{"message": {"content": contenido}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.00003},
            },
        )

    resultado = await _cliente(responder).complete_json(PEDIDO)

    cuerpo = json.loads(enviados[0].content)
    assert enviados[0].url.path == "/api/v1/chat/completions"
    assert enviados[0].headers["Authorization"] == "Bearer clave-de-prueba"
    assert cuerpo["models"] == ["modelo/principal", "modelo/respaldo"]
    assert cuerpo["reasoning"] == {"enabled": False}
    assert cuerpo["provider"] == {"require_parameters": True, "data_collection": "deny"}
    assert cuerpo["response_format"]["json_schema"]["strict"] is True
    assert resultado.data == {"resumen": "Todo en orden."}
    assert resultado.model == "modelo/respaldo"


@pytest.mark.parametrize(
    ("estado", "cuerpo"),
    [
        (429, '{"error": "rate limit"}'),
        (200, '{"choices": []}'),
        (200, "no es json"),
        (200, '{"choices": [{"message": {"content": "[1, 2]"}}]}'),
    ],
)
async def test_un_fallo_del_proveedor_deja_al_asistente_no_disponible(
    estado: int, cuerpo: str
) -> None:
    with pytest.raises(LlmUnavailable):
        await _cliente(lambda _: httpx.Response(estado, text=cuerpo)).complete_json(PEDIDO)
