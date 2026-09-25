"""Adaptador del asistente de IA por OpenRouter.

OpenRouter expone muchos modelos detrás de la misma API compatible con OpenAI.
Se usa DeepSeek Flash por precio y velocidad, con un segundo modelo de respaldo
si el primero no responde. Tres decisiones van en cada petición:

- La respuesta viene como JSON con una forma fija (`response_format`), así el
  caso de uso la valida en vez de interpretar texto libre.
- El razonamiento va apagado: una respuesta corta no lo necesita y se
  cobraría como salida.
- Solo se aceptan proveedores que respeten ese formato y que no guarden los
  datos para entrenar (`provider`).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from resthub.core.config import get_settings
from resthub.core.llm import (
    DisabledLlmClient,
    JsonCompletion,
    JsonCompletionRequest,
    LlmClient,
    LlmUnavailable,
)
from resthub.core.logs import get_logger

logger = get_logger("resthub.llm")

# Poca variación: los mismos datos deberían explicarse igual cada vez.
_TEMPERATURE = 0.2
_FENCE = "```"


class OpenRouterLlmClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        models: tuple[str, ...],
        referer: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._models = models
        self._referer = referer
        self._timeout = timeout_seconds
        # Las pruebas pasan un transporte falso: ninguna llama a OpenRouter.
        self._transport = transport

    async def complete_json(self, request: JsonCompletionRequest) -> JsonCompletion:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=self._body(request),
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "HTTP-Referer": self._referer,
                        "X-Title": "RestHub",
                    },
                )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("llm.request_failed", error=str(error))
            raise LlmUnavailable(
                "El asistente de IA no respondió. Inténtalo en un momento."
            ) from error
        return _parse(payload)

    def _body(self, request: JsonCompletionRequest) -> dict[str, Any]:
        return {
            "model": self._models[0],
            "models": list(self._models),
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                },
            },
            "reasoning": {"enabled": False},
            "max_tokens": request.max_output_tokens,
            "temperature": _TEMPERATURE,
            "provider": {"require_parameters": True, "data_collection": "deny"},
        }


def _strip_fence(content: str) -> str:
    # Algunos proveedores envuelven el JSON en un bloque de código aunque se
    # pida el formato estricto.
    text = content.strip()
    if text.startswith(_FENCE):
        text = text.split("\n", 1)[-1].rsplit(_FENCE, 1)[0]
    return text


def _parse(payload: dict[str, Any]) -> JsonCompletion:
    try:
        content = payload["choices"][0]["message"]["content"]
        data = json.loads(_strip_fence(content))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise LlmUnavailable("El asistente devolvió una respuesta que no se pudo leer.") from error
    if not isinstance(data, dict):
        raise LlmUnavailable("El asistente devolvió una respuesta que no se pudo leer.")
    usage = payload.get("usage") or {}
    model = str(payload.get("model", ""))
    logger.info(
        "llm.completed",
        model=model,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        cost=usage.get("cost"),
    )
    return JsonCompletion(data=data, model=model)


def get_llm_client() -> LlmClient:
    settings = get_settings()
    if not settings.openrouter_api_key:
        return DisabledLlmClient()
    models = tuple(
        model for model in (settings.openrouter_model, settings.openrouter_fallback_model) if model
    )
    return OpenRouterLlmClient(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        models=models,
        referer=settings.frontend_base_url,
        timeout_seconds=settings.openrouter_timeout_seconds,
    )
