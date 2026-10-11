"""Jev contra un transporte HTTP falso, y el selector que decide entre Jev y las reglas.

Ninguna prueba llama a TypeSafe. Las respuestas falsas tienen la forma que
documenta https://docs.typesafe.ai/api.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from resthub.modules.insights.adapters.ai.jev_engine import (
    NOTE_QUESTIONS,
    RESTOCK_QUESTIONS,
    WASTE_QUESTIONS,
    JevDecisionEngine,
)
from resthub.modules.insights.adapters.ai.rule_based_engine import RuleBasedDecisionEngine
from resthub.modules.insights.adapters.ai.selector import DecisionEngineSelector
from resthub.modules.insights.domain.decisions import (
    Engine,
    FallbackReason,
    KitchenNote,
    NoteType,
    RestockAction,
    SubjectType,
    Urgency,
    WasteCause,
)
from resthub.modules.insights.domain.stock import (
    IngredientFlow,
    RestockFacts,
    StockFact,
    WasteFact,
    restock_facts,
)
from resthub.modules.insights.ports.decision_engine import EngineUnavailableError

API_KEY = "ts-clave-de-prueba-que-no-debe-salir-en-los-logs"
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def _jev(handler: Handler, timeout: float = 3.0) -> JevDecisionEngine:
    return JevDecisionEngine(
        api_key=API_KEY,
        base_url="https://api.typesafe.test/",
        model="jev-latest",
        timeout_seconds=timeout,
        transport=httpx.MockTransport(handler),
    )


def _facts() -> RestockFacts:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    return restock_facts(
        StockFact(7, "Lomo de res", "g", Decimal(1500), Decimal(2000)),
        IngredientFlow(
            consumed_short=Decimal(14000),
            consumed_long=Decimal(50000),
            first_movement_at=now - timedelta(days=60),
        ),
        now,
    )


NOTE = KitchenNote(
    subject_type=SubjectType.ORDER_ITEM,
    subject_id=31,
    order_id=12,
    text="la señora es alérgica al maní",
    dish_name="Causa limeña",
)
WASTE = WasteFact(
    movement_id=5,
    ingredient_id=7,
    ingredient_name="Pescado fresco",
    unit="g",
    quantity=Decimal(500),
    cost=Decimal(14),
    reason="se malogró, olía mal",
    created_at=datetime(2026, 9, 24, tzinfo=UTC),
)


def _choice(choice: str, confidence: float, options: list[str]) -> dict[str, Any]:
    rest = (1 - confidence) / max(1, len(options) - 1)
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": {o: (confidence if o == choice else rest) for o in options},
    }


def _restock_answer(action: str = "buy_today", confidence: float = 0.86) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "action": _choice(action, confidence, list(RESTOCK_QUESTIONS["action"]["criteria"])),
            "urgency": {
                "type": "score",
                "score": 2.3,
                "confidence": 0.7,
                "legend": {
                    str(i): c for i, c in enumerate(RESTOCK_QUESTIONS["urgency"]["criteria"])
                },
                "probabilities": {"0": 0.0, "1": 0.05, "2": 0.6, "3": 0.35},
            },
        },
        "usage": {"input_tokens": 512, "output_tokens": 40},
    }


def _note_answer(noul: float, note_type: str, confidence: float) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "mentions_allergy": {"type": "noul", "noul": noul},
            "note_type": _choice(
                note_type, confidence, list(NOTE_QUESTIONS["note_type"]["criteria"])
            ),
        },
        "usage": {"input_tokens": 300, "output_tokens": 20},
    }


# -- La petición -------------------------------------------------------------


async def test_single_call_with_both_restock_questions() -> None:
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_restock_answer())

    verdict = await _jev(respond).decide_restock(_facts())

    assert len(sent) == 1
    sent_request = sent[0]
    body = json.loads(sent_request.content)
    assert str(sent_request.url) == "https://api.typesafe.test/v1/systemone"
    assert sent_request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert body["model"] == "jev-latest"
    assert body["questions"]["action"]["type"] == "choice"
    assert set(body["questions"]["action"]["criteria"]) == {a.value for a in RestockAction}
    assert body["questions"]["urgency"]["type"] == "score"
    assert len(body["questions"]["urgency"]["criteria"]) == 4
    # El estado es JSON con los números ya calculados.
    assert body["state"]["ingredient"] == {"name": "Lomo de res", "unit": "g"}
    assert body["state"]["stock_on_hand"] == 1500.0
    assert body["state"]["days_of_cover"] == 0.8
    assert body["state"]["below_minimum"] is True

    assert verdict.engine is Engine.JEV
    assert verdict.model == "jev-1.13.0"
    assert verdict.confidence == 0.86
    assert verdict.outcome.action is RestockAction.BUY_TODAY
    assert verdict.outcome.urgency is Urgency.HIGH
    assert verdict.outcome.urgency_score == 2.3
    assert verdict.details["usage"]["input_tokens"] == 512


def test_instructions_are_in_english() -> None:
    texts = json.dumps([RESTOCK_QUESTIONS, NOTE_QUESTIONS, WASTE_QUESTIONS])

    assert "restaurant" in texts and "allergy" in texts
    for word in ("comprar", "alergia", "merma", "cliente"):
        assert word not in texts


async def test_note_is_classified_with_noul_and_choice() -> None:
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_note_answer(0.97, "allergy", 0.9))

    verdict = await _jev(respond).classify_note(NOTE)

    questions = sent[0]["questions"]
    assert questions["mentions_allergy"]["type"] == "noul"
    assert questions["note_type"]["type"] == "choice"
    assert sent[0]["state"]["note"] == "la señora es alérgica al maní"
    assert sent[0]["state"]["applies_to"] == "the dish Causa limeña"
    assert verdict.outcome.mentions_allergy is True
    assert verdict.outcome.note_type is NoteType.ALLERGY
    assert verdict.outcome.allergy_probability == 0.97
    # La menos segura de las dos: 0.9 del choice contra 0.94 del noul.
    assert verdict.confidence == 0.9


async def test_waste_is_classified_with_choice() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        options = list(WASTE_QUESTIONS["cause"]["criteria"])
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {"cause": _choice("expiration", 0.95, options)},
                "usage": {"input_tokens": 200, "output_tokens": 10},
            },
        )

    verdict = await _jev(respond).classify_waste(WASTE)

    assert verdict.outcome.cause is WasteCause.EXPIRATION
    assert verdict.confidence == 0.95


# -- Fallos ------------------------------------------------------------------


async def test_on_a_429_it_retries_once_after_a_short_pause() -> None:
    attempts: list[int] = []

    def respond(_: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "limite"})
        return httpx.Response(200, json=_restock_answer())

    verdict = await _jev(respond).decide_restock(_facts())

    assert len(attempts) == 2
    assert verdict.engine is Engine.JEV


async def test_if_asked_to_wait_too_long_it_does_not_wait() -> None:
    attempts: list[int] = []

    def respond(_: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, headers={"retry-after": "30"})

    with pytest.raises(EngineUnavailableError, match="http_429"):
        await _jev(respond).decide_restock(_facts())
    assert len(attempts) == 1


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, {"detail": "clave inválida"}, "http_401"),
        (500, {"detail": "error"}, "http_500"),
        (200, "no es json", "respuesta_ilegible"),
        (200, {"answers": {}}, "respuesta_ilegible"),
        (200, {"model": "jev-1.13.0", "answers": {}}, "respuesta_invalida"),
    ],
)
async def test_bad_response_leaves_the_engine_unavailable(
    status: int, body: Any, reason: str
) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    with pytest.raises(EngineUnavailableError, match=reason):
        await _jev(respond).decide_restock(_facts())


async def test_option_that_was_not_asked_is_not_accepted() -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        answer = _restock_answer()
        answer["answers"]["action"]["choice"] = "sell_it"
        return httpx.Response(200, json=answer)

    with pytest.raises(EngineUnavailableError, match="respuesta_invalida"):
        await _jev(respond).decide_restock(_facts())


async def test_after_the_deadline_it_is_cut_off(caplog: pytest.LogCaptureFixture) -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_restock_answer())

    caplog.set_level(logging.DEBUG)
    start = asyncio.get_running_loop().time()
    with pytest.raises(EngineUnavailableError, match="tiempo_agotado"):
        await _jev(respond, timeout=0.05).decide_restock(_facts())

    assert asyncio.get_running_loop().time() - start < 1
    assert API_KEY not in caplog.text


# -- El selector -------------------------------------------------------------


def _selector(jev: JevDecisionEngine | None, threshold: float = 0.5) -> DecisionEngineSelector:
    return DecisionEngineSelector(
        rules=RuleBasedDecisionEngine(), jev=jev, min_confidence=threshold
    )


async def test_without_key_rules_decide_and_the_reason_is_recorded() -> None:
    verdict = await _selector(None).decide_restock(_facts())

    assert verdict.engine is Engine.RULES
    assert verdict.fallback is FallbackReason.NOT_CONFIGURED
    assert verdict.confidence is None
    assert verdict.outcome.action is RestockAction.BUY_TODAY


async def test_with_jev_answering_confidently_jev_decides() -> None:
    jev = _jev(lambda _: httpx.Response(200, json=_restock_answer("buy_this_week", 0.8)))

    verdict = await _selector(jev).decide_restock(_facts())

    assert verdict.engine is Engine.JEV
    assert verdict.fallback is None
    # Jev puede discrepar de las reglas: manda su respuesta.
    assert verdict.outcome.action is RestockAction.BUY_THIS_WEEK


async def test_if_jev_fails_rules_decide() -> None:
    jev = _jev(lambda _: httpx.Response(503, json={"detail": "caído"}))

    verdict = await _selector(jev).classify_waste(WASTE)

    assert verdict.engine is Engine.RULES
    assert verdict.fallback is FallbackReason.UNAVAILABLE
    assert verdict.details["jev_error"] == "http_503"
    assert verdict.outcome.cause is WasteCause.EXPIRATION


async def test_if_jev_exceeds_the_deadline_rules_decide() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_note_answer(0.9, "allergy", 0.9))

    verdict = await _selector(_jev(slow, timeout=0.05)).classify_note(NOTE)

    assert verdict.engine is Engine.RULES
    assert verdict.fallback is FallbackReason.UNAVAILABLE
    assert verdict.details["jev_error"] == "tiempo_agotado"
    assert verdict.outcome.mentions_allergy is True


async def test_with_low_confidence_rules_decide_and_what_jev_said_is_saved() -> None:
    jev = _jev(lambda _: httpx.Response(200, json=_restock_answer("wait", 0.3)))

    verdict = await _selector(jev, threshold=0.5).decide_restock(_facts())

    assert verdict.engine is Engine.RULES
    assert verdict.fallback is FallbackReason.LOW_CONFIDENCE
    assert verdict.outcome.action is RestockAction.BUY_TODAY
    assert verdict.details["jev"]["outcome"]["action"] == "wait"
    assert verdict.details["jev"]["confidence"] == 0.3


async def test_doubtful_note_goes_to_the_rules() -> None:
    # 0.55 es casi una moneda al aire: la certeza del noul es 0.1.
    jev = _jev(lambda _: httpx.Response(200, json=_note_answer(0.55, "preference", 0.9)))

    verdict = await _selector(jev).classify_note(
        KitchenNote(SubjectType.ORDER_ITEM, 1, 1, "sin mariscos", "Arroz chaufa")
    )

    assert verdict.fallback is FallbackReason.LOW_CONFIDENCE
    assert verdict.outcome.mentions_allergy is True
