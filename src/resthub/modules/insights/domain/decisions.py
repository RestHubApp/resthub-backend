"""Decisiones que toma la IA, o las reglas cuando la IA no está.

Python puro. Tres preguntas tiene hoy el negocio para un motor de decisiones:
qué hacer con cada insumo (reposición), si una nota de pedido habla de una
alergia, y por qué se tiró algo a la basura (merma). Cada respuesta es una
opción de una lista cerrada, nunca texto libre: así el código puede actuar
sobre ella y el panel mostrarla sin interpretar nada.

Toda decisión, la tome Jev o las reglas, queda guardada con lo que se preguntó,
lo que se respondió, quién respondió y con cuánta confianza (`AiDecision`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

# -- Quién decide ------------------------------------------------------------


class Engine(StrEnum):
    JEV = "jev"
    RULES = "rules"

    @property
    def label(self) -> str:
        return _ENGINE_LABELS[self]


_ENGINE_LABELS: dict[Engine, str] = {
    Engine.JEV: "Jev (IA)",
    Engine.RULES: "Reglas fijas",
}


class ConfidenceKind(StrEnum):
    """De dónde sale la seguridad de una decisión.

    Jev reparte probabilidad entre las opciones y de ahí sale un número de 0 a
    1. Las reglas no: aplican un umbral o una palabra clave y responden igual
    cada vez. Ponerles un 1.0 diría que nunca se equivocan, y una merma con un
    motivo ambiguo cae en "otro" aunque no lo sea. Por eso su confianza queda
    vacía y este tipo dice por qué.
    """

    MODEL = "model"
    RULE = "rule"

    @property
    def label(self) -> str:
        return _CONFIDENCE_LABELS[self]


_CONFIDENCE_LABELS: dict[ConfidenceKind, str] = {
    ConfidenceKind.MODEL: "Confianza del modelo",
    ConfidenceKind.RULE: "Regla fija",
}


def confidence_kind(engine: Engine) -> ConfidenceKind:
    return ConfidenceKind.MODEL if engine is Engine.JEV else ConfidenceKind.RULE


class FallbackReason(StrEnum):
    """Por qué decidieron las reglas y no Jev."""

    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    LOW_CONFIDENCE = "low_confidence"

    @property
    def label(self) -> str:
        return _FALLBACK_LABELS[self]


_FALLBACK_LABELS: dict[FallbackReason, str] = {
    FallbackReason.NOT_CONFIGURED: "La IA no está configurada",
    FallbackReason.UNAVAILABLE: "La IA no respondió a tiempo",
    FallbackReason.LOW_CONFIDENCE: "La IA no estaba segura",
}


class DecisionKind(StrEnum):
    RESTOCK = "restock"
    ORDER_NOTE = "order_note"
    WASTE_CAUSE = "waste_cause"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS: dict[DecisionKind, str] = {
    DecisionKind.RESTOCK: "Reposición de insumo",
    DecisionKind.ORDER_NOTE: "Nota de pedido",
    DecisionKind.WASTE_CAUSE: "Causa de merma",
}


class SubjectType(StrEnum):
    """Sobre qué se decidió. Con el identificador, apunta a una fila de otro módulo."""

    INGREDIENT = "ingredient"
    ORDER = "order"
    ORDER_ITEM = "order_item"
    STOCK_MOVEMENT = "stock_movement"


# -- Lo que se puede responder -----------------------------------------------


class RestockAction(StrEnum):
    BUY_TODAY = "buy_today"
    BUY_THIS_WEEK = "buy_this_week"
    WAIT = "wait"
    REVIEW_WASTE = "review_waste"

    @property
    def label(self) -> str:
        return _ACTION_LABELS[self]


_ACTION_LABELS: dict[RestockAction, str] = {
    RestockAction.BUY_TODAY: "Comprar hoy",
    RestockAction.BUY_THIS_WEEK: "Comprar esta semana",
    RestockAction.WAIT: "Esperar",
    RestockAction.REVIEW_WASTE: "Revisar la merma",
}


class Urgency(IntEnum):
    """Cuatro niveles, de menos a más. El número es el nivel de la escala de Jev."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return _URGENCY_LABELS[self]


_URGENCY_LABELS: dict[Urgency, str] = {
    Urgency.LOW: "Baja",
    Urgency.MEDIUM: "Media",
    Urgency.HIGH: "Alta",
    Urgency.CRITICAL: "Crítica",
}


class NoteType(StrEnum):
    ALLERGY = "allergy"
    PREFERENCE = "preference"
    PRIORITY = "priority"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _NOTE_LABELS[self]


_NOTE_LABELS: dict[NoteType, str] = {
    NoteType.ALLERGY: "Alergia o restricción",
    NoteType.PREFERENCE: "Preferencia",
    NoteType.PRIORITY: "Prioridad",
    NoteType.OTHER: "Otro",
}


class WasteCause(StrEnum):
    EXPIRATION = "expiration"
    MISHANDLING = "mishandling"
    CUSTOMER_RETURN = "customer_return"
    PREPARATION_ERROR = "preparation_error"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _CAUSE_LABELS[self]


_CAUSE_LABELS: dict[WasteCause, str] = {
    WasteCause.EXPIRATION: "Vencimiento",
    WasteCause.MISHANDLING: "Mala manipulación",
    WasteCause.CUSTOMER_RETURN: "Devolución del cliente",
    WasteCause.PREPARATION_ERROR: "Error de preparación",
    WasteCause.OTHER: "Otro",
}


@dataclass(frozen=True, slots=True)
class RestockOutcome:
    action: RestockAction
    urgency: Urgency
    # La posición en la escala de urgencia, que con Jev puede caer entre dos
    # niveles (1.4 es "entre media y alta, más cerca de media").
    urgency_score: float


@dataclass(frozen=True, slots=True)
class NoteOutcome:
    mentions_allergy: bool
    note_type: NoteType
    # Probabilidad de que la nota hable de una alergia; `None` con reglas.
    allergy_probability: float | None = None


@dataclass(frozen=True, slots=True)
class WasteOutcome:
    cause: WasteCause


@dataclass(frozen=True, slots=True)
class Verdict[T]:
    """Una respuesta y quién la dio."""

    outcome: T
    engine: Engine
    # El modelo que respondió de verdad (`jev-1.13.0`, no el alias pedido).
    model: str | None = None
    # De 0 a 1, derivada de cómo reparte la probabilidad el modelo. Las reglas
    # no tienen una: son siempre igual de seguras, para bien y para mal.
    confidence: float | None = None
    fallback: FallbackReason | None = None
    # Lo que respondió el motor, tal cual, para la auditoría. Con reglas, cuál
    # se aplicó; si Jev respondió con poca confianza, también su respuesta.
    details: dict[str, Any] = field(default_factory=dict)


# -- Lo que se le pregunta a un motor ----------------------------------------


@dataclass(frozen=True, slots=True)
class KitchenNote:
    """Una nota escrita por el mesero, de un plato o del pedido entero."""

    subject_type: SubjectType
    subject_id: int
    order_id: int
    text: str
    # El plato al que va la nota; vacío si es la nota general del pedido.
    dish_name: str = ""


def note_state(note: KitchenNote) -> dict[str, Any]:
    """Lo que se le muestra a Jev de una nota, y lo que se guarda como entrada."""
    return {
        "note": note.text,
        "note_language": "Spanish (Peru)",
        "written_by": "waiter taking the order",
        "applies_to": f"the dish {note.dish_name}" if note.dish_name else "the whole order",
    }


# -- El registro que queda ---------------------------------------------------


@dataclass(slots=True)
class AiDecision:
    """Una decisión guardada para auditarla. No se edita: una nueva la reemplaza."""

    restaurant_id: int
    kind: DecisionKind
    subject_type: SubjectType
    subject_id: int
    # Lo que se le mostró al motor, en el formato en que se le mostró.
    input_state: dict[str, Any]
    # Lo que decidió, más el detalle crudo de la respuesta.
    output: dict[str, Any]
    engine: Engine
    model: str | None = None
    confidence: float | None = None
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def decision_record[T](
    restaurant_id: int,
    kind: DecisionKind,
    subject_type: SubjectType,
    subject_id: int,
    state: dict[str, Any],
    verdict: Verdict[T],
    output: dict[str, Any],
) -> AiDecision:
    return AiDecision(
        restaurant_id=restaurant_id,
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        input_state=state,
        output={
            **output,
            "fallback_reason": verdict.fallback.value if verdict.fallback else None,
            "details": verdict.details,
        },
        engine=verdict.engine,
        model=verdict.model,
        confidence=verdict.confidence,
    )
