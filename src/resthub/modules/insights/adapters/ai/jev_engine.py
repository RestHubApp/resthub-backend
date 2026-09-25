"""Adaptador del motor de decisiones con Jev, de TypeSafe AI.

Jev es un modelo de "System One": no escribe texto, responde preguntas tipadas
sobre un estado. Hay tres tipos de pregunta y acá se usan los tres:

- `choice`: elige una opción de una lista cerrada y dice con cuánta confianza.
- `score`: ubica el estado en una escala ordenada de niveles descritos.
- `noul`: sí o no, como la probabilidad de que sea sí.

Cada llamada lleva un estado (JSON con los números o el texto) y varias
preguntas a la vez, que Jev evalúa en paralelo por el mismo precio de tiempo.
Instrucciones y criterios van en inglés, el idioma en que Jev es más preciso;
las notas y los motivos, que escribe el personal en castellano, van tal cual
dentro del estado, avisando el idioma.

Se llama al API HTTP con `httpx`, que ya es dependencia, y no con el SDK
`typesafe-sdk`: el SDK arrastra `httpx2`, `tenacity` y su propia versión de
Pydantic por lo que acá son tres preguntas y un reintento. El contrato está en
https://docs.typesafe.ai/api.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from resthub.core.logs import get_logger
from resthub.modules.insights.domain.decisions import (
    Engine,
    KitchenNote,
    NoteOutcome,
    NoteType,
    RestockAction,
    RestockOutcome,
    Urgency,
    Verdict,
    WasteCause,
    WasteOutcome,
    note_state,
)
from resthub.modules.insights.domain.stock import (
    RestockFacts,
    WasteFact,
    restock_state,
    waste_state,
)
from resthub.modules.insights.ports.decision_engine import EngineUnavailable

logger = get_logger("resthub.insights.jev")

ENDPOINT = "/v1/systemone"
# TypeSafe pide reintentar después de una pausa ante estos dos: límite de uso
# (429) y servicio sobrecargado (529).
RETRYABLE_STATUSES = frozenset({429, 529})
DEFAULT_RETRY_DELAY = 0.5
# Si `retry-after` pide más que esto, no se espera: deciden las reglas.
MAX_RETRY_DELAY = 1.0
# Desde qué probabilidad una nota cuenta como alergia. Por debajo de la mitad
# a propósito: no avisar de una alergia real cuesta mucho más que un aviso de
# más, y es lo que TypeSafe recomienda cuando el "sí" perdido es el caro.
ALLERGY_THRESHOLD = 0.4


# -- Preguntas ---------------------------------------------------------------

RESTOCK_QUESTIONS: dict[str, Any] = {
    "action": {
        "type": "choice",
        "instructions": (
            "Given the stock and usage numbers of this ingredient, what should the "
            "restaurant do about restocking it?"
        ),
        "criteria": {
            RestockAction.BUY_TODAY.value: (
                "Stock will not cover the next one or two days of service, or it is "
                "already out. Buy it today."
            ),
            RestockAction.BUY_THIS_WEEK.value: (
                "Stock covers a few more days but will run short within the week, or it "
                "is below the minimum. Plan a purchase in the coming days."
            ),
            RestockAction.WAIT.value: (
                "Stock comfortably covers a week or more of normal use and is above the "
                "minimum. No purchase is needed yet."
            ),
            RestockAction.REVIEW_WASTE.value: (
                "A large share of what left the stock was thrown away as waste. Check "
                "storage and handling before buying more."
            ),
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is it to restock this ingredient?",
        "criteria": [
            "Stock comfortably covers more than a week of normal use",
            "Stock covers several days, or sits below the minimum, and needs a purchase "
            "within the week",
            "Stock runs out within the next two days of service",
            "Stock is already out or will not last until the next service",
        ],
    },
}

NOTE_QUESTIONS: dict[str, Any] = {
    "mentions_allergy": {
        "type": "noul",
        "instructions": (
            "Does this order note mention an allergy, an intolerance or a dietary "
            "restriction that the kitchen must respect?"
        ),
        "criteria": {
            "true": (
                "Mentions an allergy, an intolerance or a dietary restriction, such as "
                "peanuts, seafood, gluten or celiac disease, lactose, vegetarian or vegan"
            ),
            "false": (
                "Only asks for a taste, portion, timing or serving change, with no health "
                "or dietary constraint"
            ),
        },
    },
    "note_type": {
        "type": "choice",
        "instructions": "What kind of request is this order note?",
        "criteria": {
            NoteType.ALLERGY.value: (
                "An allergy, an intolerance or a dietary restriction the kitchen must "
                "respect strictly"
            ),
            NoteType.PREFERENCE.value: (
                "A taste or preparation preference, such as no onion, less spicy, well "
                "done or sauce on the side"
            ),
            NoteType.PRIORITY.value: (
                "A timing or service priority, such as the customer is in a hurry or "
                "this dish must go out first"
            ),
            NoteType.OTHER.value: "Anything else, such as a comment for the waiter",
        },
    },
}

WASTE_QUESTIONS: dict[str, Any] = {
    "cause": {
        "type": "choice",
        "instructions": "Why was this ingredient thrown away, according to the reason given?",
        "criteria": {
            WasteCause.EXPIRATION.value: (
                "It expired, spoiled, rotted, wilted or went bad while stored"
            ),
            WasteCause.MISHANDLING.value: (
                "It was dropped, spilled, broken, contaminated or badly stored, for "
                "example after a fridge failure or being left out"
            ),
            WasteCause.CUSTOMER_RETURN.value: "A customer sent the dish back or returned it",
            WasteCause.PREPARATION_ERROR.value: (
                "The cook made a mistake while preparing it: burnt, over-salted, "
                "undercooked, wrong recipe or wrong order"
            ),
            WasteCause.OTHER.value: "Any other cause, or the reason is unclear",
        },
    },
}


# -- Respuestas --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JevResponse:
    model: str
    answers: dict[str, Any]
    usage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    confidence: float
    probabilities: dict[str, float]


def _probability(value: Any) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"fuera de rango: {number}")
    return number


def _answer(response: JevResponse, question_id: str, expected_type: str) -> dict[str, Any]:
    answer = response.answers.get(question_id)
    if not isinstance(answer, dict) or answer.get("type") != expected_type:
        raise ValueError(f"falta la respuesta {question_id} de tipo {expected_type}")
    return answer


def parse_choice(response: JevResponse, question_id: str, options: set[str]) -> ChoiceAnswer:
    answer = _answer(response, question_id, "choice")
    choice = answer.get("choice")
    if choice not in options:
        raise ValueError(f"opción desconocida en {question_id}: {choice!r}")
    probabilities = answer.get("probabilities") or {}
    return ChoiceAnswer(
        choice=str(choice),
        confidence=_probability(answer["confidence"]),
        probabilities={str(key): float(value) for key, value in probabilities.items()},
    )


def parse_score(response: JevResponse, question_id: str, levels: int) -> ScoreAnswer:
    answer = _answer(response, question_id, "score")
    score = float(answer["score"])
    if not 0.0 <= score <= levels - 1:
        raise ValueError(f"puntaje fuera de la escala en {question_id}: {score}")
    probabilities = answer.get("probabilities") or {}
    return ScoreAnswer(
        score=score,
        confidence=_probability(answer["confidence"]),
        probabilities={str(key): float(value) for key, value in probabilities.items()},
    )


def parse_noul(response: JevResponse, question_id: str) -> float:
    return _probability(_answer(response, question_id, "noul")["noul"])


def _details(response: JevResponse) -> dict[str, Any]:
    return {"answers": response.answers, "usage": response.usage}


def restock_verdict(response: JevResponse) -> Verdict[RestockOutcome]:
    action = parse_choice(response, "action", {option.value for option in RestockAction})
    urgency = parse_score(response, "urgency", len(Urgency))
    return Verdict(
        outcome=RestockOutcome(
            action=RestockAction(action.choice),
            urgency=Urgency(min(len(Urgency) - 1, max(0, round(urgency.score)))),
            urgency_score=round(urgency.score, 3),
        ),
        engine=Engine.JEV,
        model=response.model,
        # La confianza de la decisión es la de la acción: es lo que el
        # encargado va a hacer. La de la urgencia queda en el detalle.
        confidence=action.confidence,
        details=_details(response),
    )


def note_verdict(response: JevResponse) -> Verdict[NoteOutcome]:
    probability = parse_noul(response, "mentions_allergy")
    note_type = parse_choice(response, "note_type", {option.value for option in NoteType})
    # Un `noul` no trae confianza propia: la probabilidad ya lo dice todo. Qué
    # tan lejos está de 0.5 es qué tan seguro está; la decisión vale lo que la
    # menos segura de las dos respuestas.
    certainty = abs(2 * probability - 1)
    return Verdict(
        outcome=NoteOutcome(
            mentions_allergy=probability >= ALLERGY_THRESHOLD,
            note_type=NoteType(note_type.choice),
            allergy_probability=round(probability, 3),
        ),
        engine=Engine.JEV,
        model=response.model,
        confidence=round(min(note_type.confidence, certainty), 3),
        details=_details(response),
    )


def waste_verdict(response: JevResponse) -> Verdict[WasteOutcome]:
    cause = parse_choice(response, "cause", {option.value for option in WasteCause})
    return Verdict(
        outcome=WasteOutcome(cause=WasteCause(cause.choice)),
        engine=Engine.JEV,
        model=response.model,
        confidence=cause.confidence,
        details=_details(response),
    )


def _retry_delay(response: httpx.Response) -> float | None:
    """Cuánto esperar antes de reintentar, o `None` si no vale la pena."""
    header = response.headers.get("retry-after")
    if header is None:
        return DEFAULT_RETRY_DELAY
    try:
        delay = float(header)
    except ValueError:
        return DEFAULT_RETRY_DELAY
    return delay if delay <= MAX_RETRY_DELAY else None


class JevDecisionEngine:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}{ENDPOINT}"
        self._model = model
        self._timeout = timeout_seconds
        # Las pruebas pasan un transporte falso: ninguna llama a TypeSafe.
        self._transport = transport

    async def decide_restock(self, facts: RestockFacts) -> Verdict[RestockOutcome]:
        return self._parsed(
            await self._ask(restock_state(facts), RESTOCK_QUESTIONS), restock_verdict
        )

    async def classify_note(self, note: KitchenNote) -> Verdict[NoteOutcome]:
        return self._parsed(await self._ask(note_state(note), NOTE_QUESTIONS), note_verdict)

    async def classify_waste(self, waste: WasteFact) -> Verdict[WasteOutcome]:
        return self._parsed(await self._ask(waste_state(waste), WASTE_QUESTIONS), waste_verdict)

    @staticmethod
    def _parsed[T](response: JevResponse, parse: Any) -> Verdict[T]:
        try:
            verdict: Verdict[T] = parse(response)
        except (KeyError, TypeError, ValueError) as error:
            logger.warning("jev.unexpected_answer", error=str(error), model=response.model)
            raise EngineUnavailable("respuesta_invalida") from error
        return verdict

    async def _ask(self, state: dict[str, Any], questions: dict[str, Any]) -> JevResponse:
        body = {"state": state, "model": self._model, "questions": questions}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await self._post(client, body)
            if response.status_code in RETRYABLE_STATUSES:
                delay = _retry_delay(response)
                if delay is None:
                    raise EngineUnavailable(f"http_{response.status_code}")
                await asyncio.sleep(delay)
                response = await self._post(client, body)
        if response.status_code != httpx.codes.OK:
            # El cuerpo del error describe qué campo falló; nunca trae la clave.
            logger.warning("jev.http_error", status=response.status_code, body=response.text[:300])
            raise EngineUnavailable(f"http_{response.status_code}")
        try:
            payload = response.json()
            return JevResponse(
                model=str(payload["model"]),
                answers=dict(payload["answers"]),
                usage=dict(payload.get("usage") or {}),
            )
        except (KeyError, TypeError, ValueError) as error:
            logger.warning("jev.unreadable_response", error=str(error))
            raise EngineUnavailable("respuesta_ilegible") from error

    async def _post(self, client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
        try:
            # El plazo cubre la petición entera, no cada etapa: con el de
            # `httpx` solo, conectar, enviar y leer podrían sumar el triple.
            async with asyncio.timeout(self._timeout):
                return await client.post(
                    self._url,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except (TimeoutError, httpx.TimeoutException) as error:
            logger.warning("jev.timeout", timeout_seconds=self._timeout)
            raise EngineUnavailable("tiempo_agotado") from error
        except httpx.HTTPError as error:
            logger.warning("jev.request_failed", error=type(error).__name__)
            raise EngineUnavailable("sin_conexion") from error
