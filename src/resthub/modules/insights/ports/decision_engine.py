"""Puerto del motor de decisiones.

Tres preguntas cerradas, cada una con su respuesta tipada. Hay dos
implementaciones, Jev y las reglas fijas, y una tercera que elige entre ellas;
los casos de uso no saben cuál les tocó. La respuesta dice quién decidió, con
cuánta confianza y, si decidieron las reglas, por qué.
"""

from __future__ import annotations

from typing import Protocol

from resthub.modules.insights.domain.decisions import (
    KitchenNote,
    NoteOutcome,
    RestockOutcome,
    Verdict,
    WasteOutcome,
)
from resthub.modules.insights.domain.stock import RestockFacts, WasteFact


class EngineUnavailable(Exception):
    """El motor no respondió, respondió tarde o respondió algo que no se entiende."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DecisionEngine(Protocol):
    async def decide_restock(self, facts: RestockFacts) -> Verdict[RestockOutcome]: ...

    async def classify_note(self, note: KitchenNote) -> Verdict[NoteOutcome]: ...

    async def classify_waste(self, waste: WasteFact) -> Verdict[WasteOutcome]: ...
