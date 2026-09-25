"""Adaptador del motor de decisiones con reglas fijas.

Envuelve las reglas del dominio con la misma forma que Jev, para que quien
elige entre los dos no tenga que distinguirlos. Nunca falla y responde al
instante.
"""

from __future__ import annotations

from resthub.modules.insights.domain.decisions import (
    Engine,
    KitchenNote,
    NoteOutcome,
    RestockOutcome,
    Verdict,
    WasteOutcome,
)
from resthub.modules.insights.domain.rules import note_by_rules, restock_by_rules, waste_by_rules
from resthub.modules.insights.domain.stock import RestockFacts, WasteFact


class RuleBasedDecisionEngine:
    async def decide_restock(self, facts: RestockFacts) -> Verdict[RestockOutcome]:
        outcome, details = restock_by_rules(facts)
        return Verdict(outcome=outcome, engine=Engine.RULES, details=details)

    async def classify_note(self, note: KitchenNote) -> Verdict[NoteOutcome]:
        outcome, details = note_by_rules(note.text)
        return Verdict(outcome=outcome, engine=Engine.RULES, details=details)

    async def classify_waste(self, waste: WasteFact) -> Verdict[WasteOutcome]:
        outcome, details = waste_by_rules(waste.reason)
        return Verdict(outcome=outcome, engine=Engine.RULES, details=details)
