"""Quién decide: Jev cuando se puede, las reglas cuando no.

- Sin clave de TypeSafe, o si el local apagó la IA externa, las reglas,
  siempre.
- Con clave, primero Jev. Si falla, se pasa del tiempo o responde algo que no
  se entiende, deciden las reglas.
- Si Jev responde con una confianza menor al umbral configurado, también las
  reglas: Jev está diciendo que no sabe, y las reglas al menos son
  predecibles. Su respuesta no se tira: queda en el detalle de la decisión.
  Con una excepción: si Jev, aun dudando, vio una alergia en una nota, las
  reglas no la borran. No avisar de una alergia real cuesta mucho más que un
  aviso de más.

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
    NoteType,
    RestockOutcome,
    Verdict,
    WasteOutcome,
)
from resthub.modules.insights.domain.stock import RestockFacts, WasteFact
from resthub.modules.insights.ports.decision_engine import (
    DecisionEngineGateway,
    EngineUnavailableError,
)

logger = get_logger("resthub.insights")


def _rejected(verdict: Verdict[Any]) -> dict[str, Any]:
    return {
        "outcome": asdict(verdict.outcome),
        "confidence": verdict.confidence,
        "model": verdict.model,
        "details": verdict.details,
    }


def _keep_doubtful_allergy(
    jev: Verdict[NoteOutcome], rules: Verdict[NoteOutcome]
) -> Verdict[NoteOutcome]:
    """Las reglas deciden la nota, pero no quitan la alergia que Jev vio."""
    if not jev.outcome.mentions_allergy or rules.outcome.mentions_allergy:
        return rules
    return replace(
        rules,
        outcome=replace(
            rules.outcome,
            mentions_allergy=True,
            note_type=NoteType.ALLERGY,
            allergy_probability=jev.outcome.allergy_probability,
        ),
        details={**rules.details, "rule": "alergia_dudosa_de_jev"},
    )


class DecisionEngineSelector:
    def __init__(
        self,
        rules: DecisionEngineGateway,
        jev: DecisionEngineGateway | None,
        min_confidence: float,
        # Lo que se anota cuando no hay Jev: sin clave, o el local la apagó.
        without_jev: FallbackReason = FallbackReason.NOT_CONFIGURED,
    ) -> None:
        self._rules = rules
        self._jev = jev
        self._min_confidence = min_confidence
        self._without_jev = without_jev

    async def decide_restock(self, facts: RestockFacts) -> Verdict[RestockOutcome]:
        return await self._decide("restock", lambda engine: engine.decide_restock(facts))

    async def classify_note(self, note: KitchenNote) -> Verdict[NoteOutcome]:
        return await self._decide(
            "order_note", lambda engine: engine.classify_note(note), merge=_keep_doubtful_allergy
        )

    async def classify_waste(self, waste: WasteFact) -> Verdict[WasteOutcome]:
        return await self._decide("waste_cause", lambda engine: engine.classify_waste(waste))

    async def _decide[T](
        self,
        kind: str,
        ask: Callable[[DecisionEngineGateway], Awaitable[Verdict[T]]],
        # Cómo junta las reglas con lo que Jev respondió dudando; sin él, mandan las reglas.
        merge: Callable[[Verdict[T], Verdict[T]], Verdict[T]] | None = None,
    ) -> Verdict[T]:
        if self._jev is None:
            return replace(await ask(self._rules), fallback=self._without_jev)

        try:
            verdict = await ask(self._jev)
        except EngineUnavailableError as error:
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
            if merge is not None:
                fallback = merge(verdict, fallback)
            return replace(
                fallback,
                fallback=FallbackReason.LOW_CONFIDENCE,
                details={**fallback.details, "jev": _rejected(verdict)},
            )
        return verdict
