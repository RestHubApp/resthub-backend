"""Jev por OpenRouter: qué transporte se elige y cómo se llama.

Ninguna prueba llama a OpenRouter. OpenRouter sirve el mismo contrato
`/v1/systemone` que TypeSafe (https://docs.typesafe.ai/sdk/python/usage.md): la
URL base es `https://openrouter.ai/api`, la clave es la de OpenRouter y el
modelo es `~typesafe/jev-latest`. No se pudo comprobar con una clave real que la
respuesta traiga exactamente la misma forma; estas pruebas fijan la hipótesis.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from resthub.core.config import Settings
from resthub.modules.insights.adapters.ai.jev_engine import (
    PROVIDER_OPENROUTER,
    PROVIDER_TYPESAFE,
    RESTOCK_QUESTIONS,
    JevDecisionEngine,
)
from resthub.modules.insights.adapters.ai.rule_based_engine import RuleBasedDecisionEngine
from resthub.modules.insights.adapters.ai.selector import DecisionEngineSelector
from resthub.modules.insights.adapters.api.dependencies import build_jev
from resthub.modules.insights.domain.decisions import Engine, FallbackReason, RestockAction
from tests.test_jev_engine import _facts, _restock_answer

OPENROUTER_KEY = "clave-or-de-prueba-que-no-debe-salir-en-los-logs"


def _settings(**values: Any) -> Settings:
    base: dict[str, Any] = {"typesafe_api_key": "", "openrouter_api_key": ""}
    return Settings(**{**base, **values})


def _engine(handler: Any) -> JevDecisionEngine:
    return JevDecisionEngine(
        api_key=OPENROUTER_KEY,
        base_url="https://openrouter.ai/api",
        model="~typesafe/jev-latest",
        timeout_seconds=3,
        provider=PROVIDER_OPENROUTER,
        transport=httpx.MockTransport(handler),
    )


def test_typesafe_key_wins_over_openrouter() -> None:
    jev = build_jev(_settings(typesafe_api_key="ts", openrouter_api_key="or"))

    assert jev is not None
    assert jev._provider == PROVIDER_TYPESAFE
    assert jev._url == "https://api.typesafe.ai/v1/systemone"
    assert jev._model == "jev-latest"


def test_only_openrouter_key_uses_jev_through_openrouter() -> None:
    jev = build_jev(_settings(openrouter_api_key="or"))

    assert jev is not None
    assert jev._provider == PROVIDER_OPENROUTER
    assert jev._url == "https://openrouter.ai/api/v1/systemone"
    assert jev._model == "~typesafe/jev-latest"


def test_openrouter_model_is_configurable() -> None:
    jev = build_jev(_settings(openrouter_api_key="or", jev_openrouter_model="typesafe/jev-1.13"))

    assert jev is not None
    assert jev._model == "typesafe/jev-1.13"


def test_no_keys_means_no_jev() -> None:
    assert build_jev(_settings(typesafe_api_key="  ", openrouter_api_key=" ")) is None


async def test_request_is_the_system_one_contract_with_the_openrouter_key() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_restock_answer("buy_this_week", 0.8))

    verdict = await _engine(respond).decide_restock(_facts())

    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/systemone"
    assert request.headers["authorization"] == f"Bearer {OPENROUTER_KEY}"
    assert body["model"] == "~typesafe/jev-latest"
    assert body["questions"] == RESTOCK_QUESTIONS
    assert body["state"]
    assert verdict.engine is Engine.JEV
    assert verdict.details["provider"] == PROVIDER_OPENROUTER
    assert verdict.model == "jev-1.13.0"


async def test_openrouter_low_confidence_falls_back_to_rules() -> None:
    jev = _engine(lambda _: httpx.Response(200, json=_restock_answer("wait", 0.3)))
    selector = DecisionEngineSelector(rules=RuleBasedDecisionEngine(), jev=jev, min_confidence=0.5)

    verdict = await selector.decide_restock(_facts())

    assert verdict.engine is Engine.RULES
    assert verdict.fallback is FallbackReason.LOW_CONFIDENCE
    assert verdict.details["jev"]["details"]["provider"] == PROVIDER_OPENROUTER
    assert verdict.outcome.action is RestockAction.BUY_TODAY


async def test_openrouter_failure_falls_back_without_logging_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    jev = _engine(lambda _: httpx.Response(401, json={"error": {"message": "User not found."}}))
    selector = DecisionEngineSelector(rules=RuleBasedDecisionEngine(), jev=jev, min_confidence=0.5)

    with caplog.at_level(logging.DEBUG):
        verdict = await selector.decide_restock(_facts())

    assert verdict.engine is Engine.RULES
    assert verdict.fallback is FallbackReason.UNAVAILABLE
    assert verdict.details["jev_error"] == "http_401"
    assert OPENROUTER_KEY not in caplog.text
