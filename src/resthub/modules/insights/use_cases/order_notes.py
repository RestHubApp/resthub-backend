"""Notas de pedido: si hablan de una alergia, que la cocina lo vea.

El mesero escribe "sin cebolla", "rápido que se va" o "es alérgica al maní"
en el mismo campo. La cocina necesita que lo último salte a la vista. Cada nota
se clasifica una vez: si el texto no cambió, la clasificación guardada vale; si
el mesero la corrigió, se vuelve a clasificar.

Se dispara solo al enviar un pedido a cocina (ver `wiring/kitchen_notes.py`),
y el encargado puede pedirlo a mano para todos los pedidos en curso.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.realtime import EventPublisher
from resthub.core.timestamps import as_utc
from resthub.modules.insights.domain.decisions import (
    AiDecision,
    DecisionKind,
    Engine,
    KitchenNote,
    NoteOutcome,
    NoteType,
    SubjectType,
    decision_record,
    note_state,
)
from resthub.modules.insights.ports.decision_engine import DecisionEngine
from resthub.modules.insights.ports.decision_log import DecisionLog
from resthub.modules.insights.ports.kitchen_notes import KitchenNotesDirectory
from resthub.modules.insights.use_cases.shared import announce, decide_all


@dataclass(frozen=True, slots=True)
class NoteView:
    note: KitchenNote
    # `None` mientras la nota espera su clasificación.
    outcome: NoteOutcome | None = None
    engine: Engine | None = None
    confidence: float | None = None
    decision_id: int | None = None
    decided_at: datetime | None = None


def _key(note: KitchenNote) -> tuple[SubjectType, int]:
    return note.subject_type, note.subject_id


def _outcome(decision: AiDecision) -> NoteOutcome:
    output = decision.output
    probability = output.get("allergy_probability")
    return NoteOutcome(
        mentions_allergy=bool(output["mentions_allergy"]),
        note_type=NoteType(output["note_type"]),
        allergy_probability=float(probability) if probability is not None else None,
    )


def _view(note: KitchenNote, decision: AiDecision | None) -> NoteView:
    # Una clasificación de un texto que ya no es el de la nota no vale.
    if decision is None or decision.input_state.get("note") != note.text:
        return NoteView(note=note)
    return NoteView(
        note=note,
        outcome=_outcome(decision),
        engine=decision.engine,
        confidence=decision.confidence,
        decision_id=decision.id,
        decided_at=as_utc(decision.created_at),
    )


async def _latest(
    decisions: DecisionLog, restaurant_id: int, notes: Collection[KitchenNote]
) -> dict[tuple[SubjectType, int], AiDecision]:
    found: dict[tuple[SubjectType, int], AiDecision] = {}
    for subject_type in (SubjectType.ORDER_ITEM, SubjectType.ORDER):
        ids = {note.subject_id for note in notes if note.subject_type is subject_type}
        if not ids:
            continue
        latest = await decisions.latest(restaurant_id, DecisionKind.ORDER_NOTE, subject_type, ids)
        found.update({(subject_type, subject_id): d for subject_id, d in latest.items()})
    return found


@dataclass(frozen=True, slots=True)
class ClassificationRun:
    # Las recién clasificadas.
    classified: list[NoteView]
    # Las que ya tenían una clasificación vigente y no se volvieron a mirar.
    already_classified: int


class ClassifyKitchenNotes:
    def __init__(
        self, decisions: DecisionLog, engine: DecisionEngine, events: EventPublisher
    ) -> None:
        self._decisions = decisions
        self._engine = engine
        self._events = events

    async def __call__(self, restaurant_id: int, notes: list[KitchenNote]) -> ClassificationRun:
        written = [note for note in notes if note.text.strip()]
        latest = await _latest(self._decisions, restaurant_id, written)
        pending = [note for note in written if _view(note, latest.get(_key(note))).outcome is None]
        if not pending:
            return ClassificationRun(classified=[], already_classified=len(written))

        verdicts = await decide_all(pending, self._engine.classify_note)
        saved = await self._decisions.add_many(
            [
                decision_record(
                    restaurant_id,
                    DecisionKind.ORDER_NOTE,
                    note.subject_type,
                    note.subject_id,
                    note_state(note),
                    verdict,
                    {
                        "order_id": note.order_id,
                        "mentions_allergy": verdict.outcome.mentions_allergy,
                        "note_type": verdict.outcome.note_type.value,
                        "allergy_probability": verdict.outcome.allergy_probability,
                    },
                )
                for note, verdict in zip(pending, verdicts, strict=True)
            ]
        )
        # Un aviso por pedido: el tablero vuelve a pedir las notas de ese.
        for order_id in sorted({note.order_id for note in pending}):
            announce(self._events, restaurant_id, order_id)
        return ClassificationRun(
            classified=[
                _view(note, decision) for note, decision in zip(pending, saved, strict=True)
            ],
            already_classified=len(written) - len(pending),
        )


class ClassifyActiveNotes:
    """Lo que pide el encargado a mano: las notas de todos los pedidos en curso."""

    def __init__(
        self,
        notes: KitchenNotesDirectory,
        classify: ClassifyKitchenNotes,
        activity: ActivityRecorder,
    ) -> None:
        self._notes = notes
        self._classify = classify
        self._activity = activity

    async def __call__(self, restaurant_id: int, actor_id: int) -> ClassificationRun:
        run = await self._classify(restaurant_id, await self._notes.active_notes(restaurant_id))
        if run.classified:
            allergies = sum(
                1 for view in run.classified if view.outcome and view.outcome.mentions_allergy
            )
            await self._activity.record(
                restaurant_id,
                actor_id,
                ActivityKind.ORDER_NOTES_CLASSIFIED,
                f"{len(run.classified)} notas, {allergies} con alergia o restricción",
            )
        return run


class ReadNoteClassifications:
    """Las notas de unos pedidos con su clasificación vigente, si ya la tienen."""

    def __init__(self, notes: KitchenNotesDirectory, decisions: DecisionLog) -> None:
        self._notes = notes
        self._decisions = decisions

    async def __call__(self, restaurant_id: int, order_ids: Collection[int]) -> list[NoteView]:
        if not order_ids:
            return []
        notes = await self._notes.notes_for_orders(restaurant_id, order_ids)
        latest = await _latest(self._decisions, restaurant_id, notes)
        return [_view(note, latest.get(_key(note))) for note in notes]
