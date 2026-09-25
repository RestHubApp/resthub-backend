"""Causas de merma: de texto libre a una categoría que se pueda contar.

Al registrar una merma se escribe el motivo como salga ("se malogró por el
calor", "el cliente lo devolvió"). Para el reporte de causas cada motivo se
clasifica una vez y queda guardado; el libro de stock no se toca.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.realtime import EventPublisher
from resthub.modules.insights.domain.decisions import (
    DecisionKind,
    SubjectType,
    WasteCause,
    decision_record,
)
from resthub.modules.insights.domain.stock import WasteFact, waste_state
from resthub.modules.insights.ports.decision_engine import DecisionEngine
from resthub.modules.insights.ports.decision_log import DecisionLog
from resthub.modules.insights.ports.stock_directory import StockDirectory
from resthub.modules.insights.use_cases.shared import announce, decide_all

# Cuántas mermas se miran por pedido, de la más nueva a la más vieja. Un local
# chico registra unas pocas por semana; el tope evita que una base con años de
# historia sin clasificar convierta un clic en mil llamadas a la IA.
MAX_PER_RUN = 200


@dataclass(frozen=True, slots=True)
class WasteClassificationRun:
    classified: int
    by_cause: dict[WasteCause, int]
    # Las que siguen sin clasificar porque pasaban del tope de esta corrida.
    remaining: int


class ClassifyPendingWaste:
    def __init__(
        self,
        stock: StockDirectory,
        decisions: DecisionLog,
        engine: DecisionEngine,
        activity: ActivityRecorder,
        events: EventPublisher,
    ) -> None:
        self._stock = stock
        self._decisions = decisions
        self._engine = engine
        self._activity = activity
        self._events = events

    async def __call__(self, restaurant_id: int, actor_id: int) -> WasteClassificationRun:
        wastes = await self._stock.wastes(restaurant_id)
        done = await self._decisions.latest(
            restaurant_id, DecisionKind.WASTE_CAUSE, SubjectType.STOCK_MOVEMENT
        )
        pending: list[WasteFact] = [w for w in wastes if w.movement_id not in done]
        batch = pending[:MAX_PER_RUN]
        if not batch:
            return WasteClassificationRun(classified=0, by_cause={}, remaining=0)

        verdicts = await decide_all(batch, self._engine.classify_waste)
        await self._decisions.add_many(
            [
                decision_record(
                    restaurant_id,
                    DecisionKind.WASTE_CAUSE,
                    SubjectType.STOCK_MOVEMENT,
                    waste.movement_id,
                    waste_state(waste),
                    verdict,
                    {"cause": verdict.outcome.cause.value, "ingredient_id": waste.ingredient_id},
                )
                for waste, verdict in zip(batch, verdicts, strict=True)
            ]
        )
        counts = Counter(verdict.outcome.cause for verdict in verdicts)
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.WASTE_CLASSIFIED,
            ", ".join(f"{count} {cause.label.lower()}" for cause, count in counts.most_common()),
        )
        announce(self._events, restaurant_id)
        return WasteClassificationRun(
            classified=len(batch), by_cause=dict(counts), remaining=len(pending) - len(batch)
        )
