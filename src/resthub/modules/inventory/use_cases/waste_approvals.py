"""Mermas grandes: se registran como pendientes y entran al stock al aprobarlas.

Registrar una merma exige `inventory.manage`; aprobarla o rechazarla,
`inventory.approve_waste`. La que no supera el monto del local entra al libro
en el acto, como siempre.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.pagination import Page
from resthub.modules.inventory.domain.entities import Ingredient, MovementKind, StockMovement
from resthub.modules.inventory.domain.exceptions import (
    InvalidMovementError,
    WasteApprovalNotFoundError,
)
from resthub.modules.inventory.domain.waste_approvals import (
    WasteApproval,
    needs_approval,
    waste_value,
)
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.waste_approval_repository import (
    WasteApprovalPolicy,
    WasteApprovalQuery,
    WasteApprovalRepository,
)
from resthub.modules.inventory.use_cases.manage_ingredients import find_ingredient
from resthub.modules.inventory.use_cases.register_movements import (
    RegisterWaste,
    RegisterWasteCommand,
    StockChange,
)


@dataclass(frozen=True, slots=True)
class PendingWaste:
    """La merma que espera aprobación, con su insumo."""

    approval: WasteApproval
    ingredient: Ingredient


def _describe(ingredient: Ingredient, approval: WasteApproval) -> str:
    amount = f"{approval.quantity.normalize():f} {ingredient.unit.value} de {ingredient.name}"
    return f"{amount} (S/ {approval.value}): {approval.reason}"


class ReportWaste:
    """Registra una merma, o la deja pendiente si vale más que el monto del local."""

    def __init__(
        self,
        ingredients: IngredientRepository,
        approvals: WasteApprovalRepository,
        policy: WasteApprovalPolicy,
        register: RegisterWaste,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._ingredients = ingredients
        self._approvals = approvals
        self._policy = policy
        self._register = register
        self._activity = activity
        self._clock = clock

    async def __call__(self, command: RegisterWasteCommand) -> StockChange | PendingWaste:
        ingredient = await find_ingredient(
            self._ingredients, command.restaurant_id, command.ingredient_id
        )
        if command.quantity <= 0:
            message = "Indica cuánto se perdió, en positivo."
            raise InvalidMovementError(message)
        # Se arma el movimiento solo para validarlo con las reglas de siempre
        # (cantidad, motivo); el que entra al libro lo arma `RegisterWaste`.
        probe = StockMovement(
            restaurant_id=command.restaurant_id,
            ingredient_id=command.ingredient_id,
            kind=MovementKind.WASTE,
            quantity=-command.quantity,
            reason=command.reason,
            created_by=command.actor_id,
        )
        value = waste_value(-probe.quantity, ingredient.unit_cost)
        if not needs_approval(value, await self._policy.threshold(command.restaurant_id)):
            return await self._register(command)
        approval = await self._approvals.add(
            WasteApproval(
                restaurant_id=command.restaurant_id,
                ingredient_id=command.ingredient_id,
                quantity=-probe.quantity,
                unit_cost=ingredient.unit_cost,
                reason=probe.reason,
                created_by=command.actor_id,
                created_at=self._clock.now(),
            )
        )
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.WASTE_APPROVAL_REQUESTED,
            _describe(ingredient, approval),
        )
        return PendingWaste(approval=approval, ingredient=ingredient)


async def find_waste_approval(
    approvals: WasteApprovalRepository,
    restaurant_id: int,
    approval_id: int,
    *,
    for_update: bool = False,
) -> WasteApproval:
    approval = await approvals.get(restaurant_id, approval_id, for_update=for_update)
    if approval is None:
        raise WasteApprovalNotFoundError(approval_id)
    return approval


class ApproveWaste:
    """Aprobada, la merma entra al libro a costo vigente, como cualquier otra."""

    def __init__(
        self,
        approvals: WasteApprovalRepository,
        register: RegisterWaste,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._approvals = approvals
        self._register = register
        self._activity = activity
        self._clock = clock

    async def __call__(self, restaurant_id: int, actor_id: int, approval_id: int) -> WasteApproval:
        # Con bloqueo: dos aprobaciones a la vez no restan la merma dos veces.
        approval = await find_waste_approval(
            self._approvals, restaurant_id, approval_id, for_update=True
        )
        approval.approve(actor_id, self._clock.now())
        change = await self._register(
            RegisterWasteCommand(
                restaurant_id=restaurant_id,
                actor_id=actor_id,
                ingredient_id=approval.ingredient_id,
                quantity=approval.quantity,
                reason=approval.reason,
            )
        )
        approval.movement_id = change.movement.id
        saved = await self._approvals.save(approval)
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.WASTE_APPROVED,
            _describe(change.level.ingredient, saved),
        )
        return saved


class RejectWaste:
    """Rechazada, no deja movimiento: el stock queda como estaba."""

    def __init__(
        self,
        ingredients: IngredientRepository,
        approvals: WasteApprovalRepository,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._ingredients = ingredients
        self._approvals = approvals
        self._activity = activity
        self._clock = clock

    async def __call__(
        self, restaurant_id: int, actor_id: int, approval_id: int, reason: str
    ) -> WasteApproval:
        approval = await find_waste_approval(
            self._approvals, restaurant_id, approval_id, for_update=True
        )
        approval.reject(reason, actor_id, self._clock.now())
        saved = await self._approvals.save(approval)
        ingredient = await find_ingredient(self._ingredients, restaurant_id, saved.ingredient_id)
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.WASTE_REJECTED,
            f"{_describe(ingredient, saved)} — {saved.rejection_reason}",
        )
        return saved


class ListWasteApprovals:
    def __init__(self, approvals: WasteApprovalRepository) -> None:
        self._approvals = approvals

    async def __call__(self, query: WasteApprovalQuery) -> Page[WasteApproval]:
        return await self._approvals.search(query)
