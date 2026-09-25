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
from resthub.modules.insights.ports.decision_engine import EngineUnavailable

CLAVE = "ts-clave-de-prueba-que-no-debe-salir-en-los-logs"
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def _jev(handler: Handler, timeout: float = 3.0) -> JevDecisionEngine:
    return JevDecisionEngine(
        api_key=CLAVE,
        base_url="https://api.typesafe.test/",
        model="jev-latest",
        timeout_seconds=timeout,
        transport=httpx.MockTransport(handler),
    )


def _facts() -> RestockFacts:
    ahora = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    return restock_facts(
        StockFact(7, "Lomo de res", "g", Decimal("1500"), Decimal("2000")),
        IngredientFlow(
            consumed_short=Decimal("14000"),
            consumed_long=Decimal("50000"),
            first_movement_at=ahora - timedelta(days=60),
        ),
        ahora,
    )


NOTA = KitchenNote(
    subject_type=SubjectType.ORDER_ITEM,
    subject_id=31,
    order_id=12,
    text="la señora es alérgica al maní",
    dish_name="Causa limeña",
)
MERMA = WasteFact(
    movement_id=5,
    ingredient_id=7,
    ingredient_name="Pescado fresco",
    unit="g",
    quantity=Decimal("500"),
    cost=Decimal("14"),
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


async def test_una_sola_llamada_con_las_dos_preguntas_de_reposicion() -> None:
    enviados: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        enviados.append(request)
        return httpx.Response(200, json=_restock_answer())

    veredicto = await _jev(responder).decide_restock(_facts())

    assert len(enviados) == 1
    pedido = enviados[0]
    cuerpo = json.loads(pedido.content)
    assert str(pedido.url) == "https://api.typesafe.test/v1/systemone"
    assert pedido.headers["Authorization"] == f"Bearer {CLAVE}"
    assert cuerpo["model"] == "jev-latest"
    assert cuerpo["questions"]["action"]["type"] == "choice"
    assert set(cuerpo["questions"]["action"]["criteria"]) == {a.value for a in RestockAction}
    assert cuerpo["questions"]["urgency"]["type"] == "score"
    assert len(cuerpo["questions"]["urgency"]["criteria"]) == 4
    # El estado es JSON con los números ya calculados.
    assert cuerpo["state"]["ingredient"] == {"name": "Lomo de res", "unit": "g"}
    assert cuerpo["state"]["stock_on_hand"] == 1500.0
    assert cuerpo["state"]["days_of_cover"] == 0.8
    assert cuerpo["state"]["below_minimum"] is True

    assert veredicto.engine is Engine.JEV
    assert veredicto.model == "jev-1.13.0"
    assert veredicto.confidence == 0.86
    assert veredicto.outcome.action is RestockAction.BUY_TODAY
    assert veredicto.outcome.urgency is Urgency.HIGH
    assert veredicto.outcome.urgency_score == 2.3
    assert veredicto.details["usage"]["input_tokens"] == 512


def test_las_instrucciones_van_en_ingles() -> None:
    textos = json.dumps([RESTOCK_QUESTIONS, NOTE_QUESTIONS, WASTE_QUESTIONS])

    assert "restaurant" in textos and "allergy" in textos
    for palabra in ("comprar", "alergia", "merma", "cliente"):
        assert palabra not in textos


async def test_una_nota_se_clasifica_con_noul_y_choice() -> None:
    enviados: list[dict[str, Any]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        enviados.append(json.loads(request.content))
        return httpx.Response(200, json=_note_answer(0.97, "allergy", 0.9))

    veredicto = await _jev(responder).classify_note(NOTA)

    preguntas = enviados[0]["questions"]
    assert preguntas["mentions_allergy"]["type"] == "noul"
    assert preguntas["note_type"]["type"] == "choice"
    assert enviados[0]["state"]["note"] == "la señora es alérgica al maní"
    assert enviados[0]["state"]["applies_to"] == "the dish Causa limeña"
    assert veredicto.outcome.mentions_allergy is True
    assert veredicto.outcome.note_type is NoteType.ALLERGY
    assert veredicto.outcome.allergy_probability == 0.97
    # La menos segura de las dos: 0.9 del choice contra 0.94 del noul.
    assert veredicto.confidence == 0.9


async def test_una_merma_se_clasifica_con_choice() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        opciones = list(WASTE_QUESTIONS["cause"]["criteria"])
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {"cause": _choice("expiration", 0.95, opciones)},
                "usage": {"input_tokens": 200, "output_tokens": 10},
            },
        )

    veredicto = await _jev(responder).classify_waste(MERMA)

    assert veredicto.outcome.cause is WasteCause.EXPIRATION
    assert veredicto.confidence == 0.95


# -- Fallos ------------------------------------------------------------------


async def test_ante_un_429_reintenta_una_vez_tras_una_pausa_breve() -> None:
    intentos: list[int] = []

    def responder(_: httpx.Request) -> httpx.Response:
        intentos.append(1)
        if len(intentos) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "limite"})
        return httpx.Response(200, json=_restock_answer())

    veredicto = await _jev(responder).decide_restock(_facts())

    assert len(intentos) == 2
    assert veredicto.engine is Engine.JEV


async def test_si_pide_esperar_demasiado_no_espera() -> None:
    intentos: list[int] = []

    def responder(_: httpx.Request) -> httpx.Response:
        intentos.append(1)
        return httpx.Response(429, headers={"retry-after": "30"})

    with pytest.raises(EngineUnavailable, match="http_429"):
        await _jev(responder).decide_restock(_facts())
    assert len(intentos) == 1


@pytest.mark.parametrize(
    ("estado", "cuerpo", "motivo"),
    [
        (401, {"detail": "clave inválida"}, "http_401"),
        (500, {"detail": "error"}, "http_500"),
        (200, "no es json", "respuesta_ilegible"),
        (200, {"answers": {}}, "respuesta_ilegible"),
        (200, {"model": "jev-1.13.0", "answers": {}}, "respuesta_invalida"),
    ],
)
async def test_una_respuesta_mala_deja_al_motor_no_disponible(
    estado: int, cuerpo: Any, motivo: str
) -> None:
    def responder(_: httpx.Request) -> httpx.Response:
        if isinstance(cuerpo, str):
            return httpx.Response(estado, text=cuerpo)
        return httpx.Response(estado, json=cuerpo)

    with pytest.raises(EngineUnavailable, match=motivo):
        await _jev(responder).decide_restock(_facts())


async def test_una_opcion_que_no_se_pregunto_no_se_acepta() -> None:
    def responder(_: httpx.Request) -> httpx.Response:
        respuesta = _restock_answer()
        respuesta["answers"]["action"]["choice"] = "sell_it"
        return httpx.Response(200, json=respuesta)

    with pytest.raises(EngineUnavailable, match="respuesta_invalida"):
        await _jev(responder).decide_restock(_facts())


async def test_pasado_el_plazo_se_corta(caplog: pytest.LogCaptureFixture) -> None:
    async def responder(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_restock_answer())

    caplog.set_level(logging.DEBUG)
    inicio = asyncio.get_running_loop().time()
    with pytest.raises(EngineUnavailable, match="tiempo_agotado"):
        await _jev(responder, timeout=0.05).decide_restock(_facts())

    assert asyncio.get_running_loop().time() - inicio < 1
    assert CLAVE not in caplog.text


# -- El selector -------------------------------------------------------------


def _selector(jev: JevDecisionEngine | None, umbral: float = 0.5) -> DecisionEngineSelector:
    return DecisionEngineSelector(rules=RuleBasedDecisionEngine(), jev=jev, min_confidence=umbral)


async def test_sin_clave_deciden_las_reglas_y_queda_dicho_por_que() -> None:
    veredicto = await _selector(None).decide_restock(_facts())

    assert veredicto.engine is Engine.RULES
    assert veredicto.fallback is FallbackReason.NOT_CONFIGURED
    assert veredicto.confidence is None
    assert veredicto.outcome.action is RestockAction.BUY_TODAY


async def test_con_jev_respondiendo_seguro_decide_jev() -> None:
    jev = _jev(lambda _: httpx.Response(200, json=_restock_answer("buy_this_week", 0.8)))

    veredicto = await _selector(jev).decide_restock(_facts())

    assert veredicto.engine is Engine.JEV
    assert veredicto.fallback is None
    # Jev puede discrepar de las reglas: manda su respuesta.
    assert veredicto.outcome.action is RestockAction.BUY_THIS_WEEK


async def test_si_jev_falla_deciden_las_reglas() -> None:
    jev = _jev(lambda _: httpx.Response(503, json={"detail": "caído"}))

    veredicto = await _selector(jev).classify_waste(MERMA)

    assert veredicto.engine is Engine.RULES
    assert veredicto.fallback is FallbackReason.UNAVAILABLE
    assert veredicto.details["jev_error"] == "http_503"
    assert veredicto.outcome.cause is WasteCause.EXPIRATION


async def test_si_jev_se_pasa_del_plazo_deciden_las_reglas() -> None:
    async def lento(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_note_answer(0.9, "allergy", 0.9))

    veredicto = await _selector(_jev(lento, timeout=0.05)).classify_note(NOTA)

    assert veredicto.engine is Engine.RULES
    assert veredicto.fallback is FallbackReason.UNAVAILABLE
    assert veredicto.details["jev_error"] == "tiempo_agotado"
    assert veredicto.outcome.mentions_allergy is True


async def test_con_poca_confianza_deciden_las_reglas_y_se_guarda_lo_que_dijo_jev() -> None:
    jev = _jev(lambda _: httpx.Response(200, json=_restock_answer("wait", 0.3)))

    veredicto = await _selector(jev, umbral=0.5).decide_restock(_facts())

    assert veredicto.engine is Engine.RULES
    assert veredicto.fallback is FallbackReason.LOW_CONFIDENCE
    assert veredicto.outcome.action is RestockAction.BUY_TODAY
    assert veredicto.details["jev"]["outcome"]["action"] == "wait"
    assert veredicto.details["jev"]["confidence"] == 0.3


async def test_una_nota_dudosa_pasa_a_las_reglas() -> None:
    # 0.55 es casi una moneda al aire: la certeza del noul es 0.1.
    jev = _jev(lambda _: httpx.Response(200, json=_note_answer(0.55, "preference", 0.9)))

    veredicto = await _selector(jev).classify_note(
        KitchenNote(SubjectType.ORDER_ITEM, 1, 1, "sin mariscos", "Arroz chaufa")
    )

    assert veredicto.fallback is FallbackReason.LOW_CONFIDENCE
    assert veredicto.outcome.mentions_allergy is True
