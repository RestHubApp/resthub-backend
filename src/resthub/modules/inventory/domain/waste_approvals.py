"""Mermas grandes que esperan la aprobación del encargado.

Python puro. Cada local fija un monto (`waste_approval_threshold`, en soles):
una merma que vale más que eso, a costo vigente, no toca el stock al
registrarla. Queda pendiente, y solo al aprobarla entra al libro. Si se
rechaza, no deja movimiento. Sin monto (`None`) ninguna merma pide aprobación.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from resthub.core.clock import utc_now
from resthub.modules.inventory.domain.exceptions import (
    InvalidWasteApprovalError,
    WasteAlreadyDecidedError,
)

MAX_REJECTION_LENGTH = 200
_CENT = Decimal("0.01")


class WasteApprovalStatus(StrEnum):
    PENDING = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


_STATUS_LABELS: dict[WasteApprovalStatus, str] = {
    WasteApprovalStatus.PENDING: "Por aprobar",
    WasteApprovalStatus.APPROVED: "Aprobada",
    WasteApprovalStatus.REJECTED: "Rechazada",
}


def waste_value(quantity: Decimal, unit_cost: Decimal) -> Decimal:
    """Lo que vale una merma, en soles a dos decimales."""
    return (quantity * unit_cost).quantize(_CENT, ROUND_HALF_UP)


def needs_approval(value: Decimal, threshold: Decimal | None) -> bool:
    """Estrictamente por encima del monto: una merma de justo el monto pasa sola."""
    return threshold is not None and value > threshold


@dataclass(slots=True)
class WasteApproval:
    restaurant_id: int
    ingredient_id: int
    # Lo que se perdió, en positivo y en la unidad del insumo.
    quantity: Decimal
    # El costo vigente al registrarla: con él se decidió que pedía aprobación.
    unit_cost: Decimal
    reason: str
    created_by: int
    status: WasteApprovalStatus = WasteApprovalStatus.PENDING
    id: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    decided_by: int | None = None
    decided_at: datetime | None = None
    rejection_reason: str = ""
    # El movimiento de merma que dejó al aprobarse.
    movement_id: int | None = None

    @property
    def value(self) -> Decimal:
        return waste_value(self.quantity, self.unit_cost)

    def approve(self, actor_id: int, now: datetime) -> None:
        self._ensure_pending()
        self.status = WasteApprovalStatus.APPROVED
        self.decided_by = actor_id
        self.decided_at = now

    def reject(self, reason: str, actor_id: int, now: datetime) -> None:
        self._ensure_pending()
        text = " ".join(reason.split())
        if not text:
            message = "Indica por qué se rechaza la merma."
            raise InvalidWasteApprovalError(message)
        if len(text) > MAX_REJECTION_LENGTH:
            message = f"El motivo del rechazo admite {MAX_REJECTION_LENGTH} caracteres."
            raise InvalidWasteApprovalError(message)
        self.status = WasteApprovalStatus.REJECTED
        self.rejection_reason = text
        self.decided_by = actor_id
        self.decided_at = now

    def _ensure_pending(self) -> None:
        if self.status is not WasteApprovalStatus.PENDING:
            raise WasteAlreadyDecidedError(self.status.label)
