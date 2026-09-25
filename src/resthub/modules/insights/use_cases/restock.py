"""Sugerencias de compra de insumos.

Los números los calcula el código (stock, consumo por día, cobertura,
tendencia, merma); qué hacer con ellos lo decide el motor: Jev si está
configurado y responde con confianza, las reglas fijas si no.

Consultar no llama a la IA: muestra la última decisión guardada de cada insumo
junto a sus números de ahora. Actualizar sí pregunta, insumo por insumo, y
guarda cada respuesta. Un insumo que nunca se decidió se muestra con lo que
dirían las reglas, sin guardarlo, para que la lista nunca salga vacía.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.timestamps import as_utc
from resthub.modules.insights.domain.decisions import (
    AiDecision,
    DecisionKind,
    Engine,
    FallbackReason,
    RestockAction,
    RestockOutcome,
    SubjectType,
    Urgency,
    Verdict,
    decision_record,
)
from resthub.modules.insights.domain.explanations import explain_restock, with_decimal_point
from resthub.modules.insights.domain.rules import restock_by_rules
from resthub.modules.insights.domain.stock import (
    LONG_WINDOW_DAYS,
    SHORT_WINDOW_DAYS,
    IngredientFlow,
    RestockFacts,
    restock_facts,
    restock_state,
)
from resthub.modules.insights.ports.decision_engine import DecisionEngine
from resthub.modules.insights.ports.decision_log import DecisionLog
from resthub.modules.insights.ports.stock_directory import FlowWindows, StockDirectory
from resthub.modules.insights.use_cases.shared import decide_all

# Una sugerencia de hace más de un día ya no describe el almacén de hoy.
STALE_AFTER = timedelta(hours=24)

# Qué va primero en la lista a igual urgencia: lo que hay que hacer hoy.
_ACTION_ORDER = {
    RestockAction.BUY_TODAY: 0,
    RestockAction.REVIEW_WASTE: 1,
    RestockAction.BUY_THIS_WEEK: 2,
    RestockAction.WAIT: 3,
}


@dataclass(frozen=True, slots=True)
class RestockItem:
    facts: RestockFacts
    verdict: Verdict[RestockOutcome]
    explanation: str
    # Sin decisión guardada, los dos quedan en `None`: es lo que dirían las
    # reglas con los números de ahora.
    decision_id: int | None = None
    decided_at: datetime | None = None
    # Hubo una compra después de decidir, o la decisión tiene más de un día.
    is_stale: bool = False


def _sorted(items: list[RestockItem]) -> list[RestockItem]:
    def key(item: RestockItem) -> tuple[int, int, float, str]:
        coverage = item.facts.coverage_days
        return (
            -item.verdict.outcome.urgency,
            _ACTION_ORDER[item.verdict.outcome.action],
            float(coverage) if coverage is not None else float("inf"),
            item.facts.name,
        )

    return sorted(items, key=key)


async def _current_facts(
    stock: StockDirectory, restaurant_id: int, now: datetime
) -> tuple[list[RestockFacts], dict[int, IngredientFlow]]:
    levels = await stock.stock_levels(restaurant_id)
    flows = await stock.flows(
        restaurant_id,
        FlowWindows(
            short_since=now - timedelta(days=SHORT_WINDOW_DAYS),
            long_since=now - timedelta(days=LONG_WINDOW_DAYS),
        ),
    )
    facts = [
        restock_facts(level, flows.get(level.ingredient_id, IngredientFlow()), now)
        for level in levels
    ]
    return facts, flows


def verdict_from(decision: AiDecision) -> Verdict[RestockOutcome]:
    output = decision.output
    fallback = output.get("fallback_reason")
    return Verdict(
        outcome=RestockOutcome(
            action=RestockAction(output["action"]),
            urgency=Urgency(int(output["urgency"])),
            urgency_score=float(output["urgency_score"]),
        ),
        engine=decision.engine,
        model=decision.model,
        confidence=decision.confidence,
        fallback=FallbackReason(fallback) if fallback else None,
        details=dict(output.get("details") or {}),
    )


def _rules_preview(facts: RestockFacts) -> RestockItem:
    outcome, details = restock_by_rules(facts)
    verdict = Verdict(outcome=outcome, engine=Engine.RULES, details=details)
    return RestockItem(facts=facts, verdict=verdict, explanation=explain_restock(facts, verdict))


class ReadRestock:
    def __init__(self, stock: StockDirectory, decisions: DecisionLog) -> None:
        self._stock = stock
        self._decisions = decisions

    async def __call__(self, restaurant_id: int) -> list[RestockItem]:
        now = datetime.now(UTC)
        facts, flows = await _current_facts(self._stock, restaurant_id, now)
        stored = await self._decisions.latest(
            restaurant_id,
            DecisionKind.RESTOCK,
            SubjectType.INGREDIENT,
            {fact.ingredient_id for fact in facts},
        )
        items: list[RestockItem] = []
        for fact in facts:
            decision = stored.get(fact.ingredient_id)
            if decision is None:
                items.append(_rules_preview(fact))
                continue
            decided_at = as_utc(decision.created_at)
            last_purchase = flows.get(fact.ingredient_id, IngredientFlow()).last_purchase_at
            items.append(
                RestockItem(
                    facts=fact,
                    verdict=verdict_from(decision),
                    explanation=with_decimal_point(str(decision.output.get("explanation", ""))),
                    decision_id=decision.id,
                    decided_at=decided_at,
                    is_stale=now - decided_at > STALE_AFTER
                    or (last_purchase is not None and as_utc(last_purchase) > decided_at),
                )
            )
        return _sorted(items)


class RefreshRestock:
    """Pregunta por cada insumo activo y guarda las respuestas.

    Una llamada al motor por insumo, varias a la vez. Con Jev cada llamada
    lleva las dos preguntas (qué hacer y con cuánta urgencia) sobre el mismo
    estado, que es como TypeSafe recomienda usarlo.
    """

    def __init__(
        self,
        stock: StockDirectory,
        decisions: DecisionLog,
        engine: DecisionEngine,
        activity: ActivityRecorder,
    ) -> None:
        self._stock = stock
        self._decisions = decisions
        self._engine = engine
        self._activity = activity

    async def __call__(self, restaurant_id: int, actor_id: int) -> list[RestockItem]:
        now = datetime.now(UTC)
        facts, _ = await _current_facts(self._stock, restaurant_id, now)
        verdicts = await decide_all(facts, self._engine.decide_restock)
        explanations = [
            explain_restock(fact, verdict) for fact, verdict in zip(facts, verdicts, strict=True)
        ]
        saved = await self._decisions.add_many(
            [
                decision_record(
                    restaurant_id,
                    DecisionKind.RESTOCK,
                    SubjectType.INGREDIENT,
                    fact.ingredient_id,
                    restock_state(fact),
                    verdict,
                    {
                        "action": verdict.outcome.action.value,
                        "urgency": int(verdict.outcome.urgency),
                        "urgency_score": verdict.outcome.urgency_score,
                        "explanation": explanation,
                    },
                )
                for fact, verdict, explanation in zip(facts, verdicts, explanations, strict=True)
            ]
        )
        to_buy = sum(1 for v in verdicts if v.outcome.action is RestockAction.BUY_TODAY)
        by_jev = sum(1 for v in verdicts if v.engine is Engine.JEV)
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.RESTOCK_REFRESHED,
            f"{len(facts)} insumos, {to_buy} para comprar hoy; {by_jev} decididos por Jev",
        )
        return _sorted(
            [
                RestockItem(
                    facts=fact,
                    verdict=verdict,
                    explanation=explanation,
                    decision_id=decision.id,
                    decided_at=as_utc(decision.created_at),
                )
                for fact, verdict, explanation, decision in zip(
                    facts, verdicts, explanations, saved, strict=True
                )
            ]
        )
