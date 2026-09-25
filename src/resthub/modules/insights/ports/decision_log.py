"""Puerto de persistencia de las decisiones de IA.

Solo se agrega y se consulta: una decisión no se edita. Si el mismo asunto se
vuelve a decidir, la nueva queda al lado de la vieja y la lectura toma la
última.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.modules.insights.domain.decisions import (
    AiDecision,
    DecisionKind,
    Engine,
    SubjectType,
)


@dataclass(frozen=True, slots=True)
class DecisionQuery:
    # Obligatorio y primero: una búsqueda sin restaurante no se puede escribir.
    restaurant_id: int
    kind: DecisionKind | None = None
    engine: Engine | None = None
    subject_type: SubjectType | None = None
    subject_id: int | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class DecisionLog(Protocol):
    async def add_many(self, decisions: list[AiDecision]) -> list[AiDecision]: ...

    async def latest(
        self,
        restaurant_id: int,
        kind: DecisionKind,
        subject_type: SubjectType,
        subject_ids: Collection[int] | None = None,
    ) -> dict[int, AiDecision]:
        """La última decisión de cada asunto, por su identificador.

        Sin `subject_ids`, la de todos los asuntos de ese tipo en el restaurante.
        """
        ...

    async def search(self, query: DecisionQuery) -> Page[AiDecision]:
        """De la más nueva a la más vieja."""
        ...
