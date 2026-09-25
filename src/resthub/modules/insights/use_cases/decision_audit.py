"""Auditoría de las decisiones: qué se preguntó, qué se respondió y quién."""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.pagination import Page
from resthub.modules.insights.domain.decisions import AiDecision
from resthub.modules.insights.ports.decision_log import DecisionLog, DecisionQuery
from resthub.modules.insights.ports.subject_directory import SubjectDescription, SubjectDirectory


@dataclass(frozen=True, slots=True)
class DecisionEntry:
    decision: AiDecision
    # Cómo se nombra en el local el asunto de la decisión.
    subject: SubjectDescription


class ListDecisions:
    def __init__(self, decisions: DecisionLog, subjects: SubjectDirectory) -> None:
        self._decisions = decisions
        self._subjects = subjects

    async def __call__(self, query: DecisionQuery) -> Page[DecisionEntry]:
        page = await self._decisions.search(query)
        described = await self._subjects.describe(
            query.restaurant_id, {(d.subject_type, d.subject_id) for d in page.items}
        )
        return Page(
            items=[
                DecisionEntry(
                    decision=decision,
                    subject=described.get(
                        (decision.subject_type, decision.subject_id), SubjectDescription()
                    ),
                )
                for decision in page.items
            ],
            total=page.total,
        )
