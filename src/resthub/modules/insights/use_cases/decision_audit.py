"""Auditoría de las decisiones: qué se preguntó, qué se respondió y quién."""

from __future__ import annotations

from resthub.core.pagination import Page
from resthub.modules.insights.domain.decisions import AiDecision
from resthub.modules.insights.ports.decision_log import DecisionLog, DecisionQuery


class ListDecisions:
    def __init__(self, decisions: DecisionLog) -> None:
        self._decisions = decisions

    async def __call__(self, query: DecisionQuery) -> Page[AiDecision]:
        return await self._decisions.search(query)
