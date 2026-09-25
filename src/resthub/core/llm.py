"""Modelos de lenguaje: el puerto.

La IA ayuda al encargado (sugerencias de compra, explicaciones de una
predicción) y nunca decide por él. Este módulo solo dice qué se le pide a un
modelo: una respuesta JSON con una forma dada. Cómo se pide, y a quién, es un
adaptador: hoy `llm_openrouter`.

Python puro: lo importan los casos de uso.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LlmUnavailable(Exception):
    """El asistente no está configurado, no respondió o respondió algo ilegible."""


@dataclass(frozen=True, slots=True)
class JsonCompletionRequest:
    system: str
    user: str
    schema_name: str
    schema: dict[str, Any]
    max_output_tokens: int = 1200


@dataclass(frozen=True, slots=True)
class JsonCompletion:
    data: dict[str, Any]
    # El modelo que respondió de verdad: con respaldo, puede no ser el pedido.
    model: str


class LlmClient(Protocol):
    async def complete_json(self, request: JsonCompletionRequest) -> JsonCompletion: ...


class DisabledLlmClient:
    """Sin clave configurada, el asistente queda apagado y el resto funciona igual."""

    async def complete_json(self, request: JsonCompletionRequest) -> JsonCompletion:
        raise LlmUnavailable("El asistente de IA no está configurado en este servidor.")
