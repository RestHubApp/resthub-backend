"""Quién decide: Jev cuando se puede, las reglas cuando no.

- Sin clave de TypeSafe, las reglas, siempre.
- Con clave, primero Jev. Si falla, se pasa del tiempo o responde algo que no
  se entiende, deciden las reglas.
- Si Jev responde con una confianza menor al umbral configurado, también las
  reglas: Jev está diciendo que no sabe, y las reglas al menos son
  predecibles. Su respuesta no se tira: queda en el detalle de la decisión.

Cada vez que deciden las reglas en lugar de Jev queda anotado por qué, en la
decisión guardada y en el log, para que el panel lo muestre y se pueda ajustar
el umbral con datos.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from typing import Any

from resthub.core.logs import get_logger
from resthub.modules.insights.domain.decisions import (
    FallbackReason,
    KitchenNote,
    NoteOutcome,
    RestockOutcome,
    Verdict,
    WasteOutcome,
)
from resthub.modules.insights.domain.stock import RestockFacts, WasteFact
from resthub.modules.insights.ports.decision_engine import DecisionEngine, EngineUnavailable

logger = get_logger("resthub.insights")


def _rejected(verdict: Verdict[Any]) -> dict[str, Any]:
    return {
        "outcome": asdict(verdict.outcome),
        "confidence": verdict.confidence,
        "model": verdict.model,
        "details": verdict.details,
    }


class DecisionEngineSelector:
    def __init__(
        self, rules: DecisionEngine, jev: DecisionEngine | None, min_confidence: float
    ) -> None:
        self._rules = rules
        self._jev = jev
        self._min_confidence = min_confidence

    async def decide_restock(self, facts: RestockFacts) -> Verdict[RestockOutcome]:
        return await self._decide("restock", lambda engine: engine.decide_restock(facts))

    async def classify_note(self, note: KitchenNote) -> Verdict[NoteOutcome]:
        return await self._decide("order_note", lambda engine: engine.classify_note(note))

    async def classify_waste(self, waste: WasteFact) -> Verdict[WasteOutcome]:
        return await self._decide("waste_cause", lambda engine: engine.classify_waste(waste))

    async def _decide[T](
        self, kind: str, ask: Callable[[DecisionEngine], Awaitable[Verdict[T]]]
    ) -> Verdict[T]:
        if self._jev is None:
            return replace(await ask(self._rules), fallback=FallbackReason.NOT_CONFIGURED)

        try:
            verdict = await ask(self._jev)
        except EngineUnavailable as error:
            logger.warning("insights.jev_unavailable", kind=kind, reason=error.reason)
            fallback = await ask(self._rules)
            return replace(
                fallback,
                fallback=FallbackReason.UNAVAILABLE,
                details={**fallback.details, "jev_error": error.reason},
            )

        confidence = verdict.confidence if verdict.confidence is not None else 0.0
        if confidence < self._min_confidence:
            logger.info(
                "insights.jev_low_confidence",
                kind=kind,
                confidence=confidence,
                threshold=self._min_confidence,
            )
            fallback = await ask(self._rules)
            return replace(
                fallback,
                fallback=FallbackReason.LOW_CONFIDENCE,
                details={**fallback.details, "jev": _rejected(verdict)},
            )
        return verdict
