"""Persistencia de las decisiones de IA."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page
from resthub.core.timestamps import as_utc
from resthub.modules.insights.adapters.persistence.models import AiDecisionRow
from resthub.modules.insights.domain.decisions import (
    AiDecision,
    DecisionKind,
    Engine,
    SubjectType,
)
from resthub.modules.insights.ports.decision_log import DecisionQuery


def _to_entity(row: AiDecisionRow) -> AiDecision:
    return AiDecision(
        id=row.id,
        restaurant_id=row.restaurant_id,
        kind=DecisionKind(row.kind),
        subject_type=SubjectType(row.subject_type),
        subject_id=row.subject_id,
        input_state=dict(row.input_state),
        output=dict(row.output),
        engine=Engine(row.engine),
        model=row.model,
        confidence=row.confidence,
        created_at=as_utc(row.created_at),
    )


def _to_row(decision: AiDecision) -> AiDecisionRow:
    return AiDecisionRow(
        restaurant_id=decision.restaurant_id,
        kind=decision.kind.value,
        subject_type=decision.subject_type.value,
        subject_id=decision.subject_id,
        input_state=decision.input_state,
        output=decision.output,
        engine=decision.engine.value,
        model=decision.model,
        confidence=decision.confidence,
        created_at=decision.created_at,
    )


class SqlAlchemyDecisionLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_many(self, decisions: list[AiDecision]) -> list[AiDecision]:
        rows = [_to_row(decision) for decision in decisions]
        self._session.add_all(rows)
        await self._session.flush()
        return [_to_entity(row) for row in rows]

    async def latest(
        self,
        restaurant_id: int,
        kind: DecisionKind,
        subject_type: SubjectType,
        subject_ids: Collection[int] | None = None,
    ) -> dict[int, AiDecision]:
        if subject_ids is not None and not subject_ids:
            return {}
        # El identificador crece con el tiempo: el mayor de cada asunto es el
        # último que se decidió, sin depender de la precisión de la hora.
        newest = (
            select(func.max(AiDecisionRow.id))
            .where(
                AiDecisionRow.restaurant_id == restaurant_id,
                AiDecisionRow.kind == kind.value,
                AiDecisionRow.subject_type == subject_type.value,
            )
            .group_by(AiDecisionRow.subject_id)
        )
        if subject_ids is not None:
            newest = newest.where(AiDecisionRow.subject_id.in_(list(subject_ids)))
        result = await self._session.execute(
            select(AiDecisionRow).where(AiDecisionRow.id.in_(newest))
        )
        return {row.subject_id: _to_entity(row) for row in result.scalars().all()}

    async def search(self, query: DecisionQuery) -> Page[AiDecision]:
        base = select(AiDecisionRow).where(AiDecisionRow.restaurant_id == query.restaurant_id)
        if query.kind is not None:
            base = base.where(AiDecisionRow.kind == query.kind.value)
        if query.engine is not None:
            base = base.where(AiDecisionRow.engine == query.engine.value)
        if query.subject_type is not None:
            base = base.where(AiDecisionRow.subject_type == query.subject_type.value)
        if query.subject_id is not None:
            base = base.where(AiDecisionRow.subject_id == query.subject_id)
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        result = await self._session.execute(
            base.order_by(AiDecisionRow.id.desc()).limit(query.limit).offset(query.offset)
        )
        return Page(items=[_to_entity(row) for row in result.scalars().all()], total=total)
