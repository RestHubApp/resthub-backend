"""Descuentos y cortesías: lo que cambia cuánto se cobra antes de cobrar.

El mesero descuenta hasta el tope que fija el encargado, siempre con motivo.
Por encima del tope, y las cortesías (un plato que invita la casa), solo quien
tiene `orders.discount_any`: el encargado. Todo queda en la bitácora.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.core.realtime import EventPublisher
from resthub.modules.orders.domain.orders import Order
from resthub.modules.orders.ports.discount_policy import DiscountPolicy
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.use_cases.shared import (
    announce,
    ensure_owns_or_manages,
    find_visible_order,
)


def can_discount_freely(principal: Principal) -> bool:
    return Permission.ORDERS_DISCOUNT_ANY in principal.permissions


@dataclass(frozen=True, slots=True)
class ApplyDiscountCommand:
    actor: Principal
    order_id: int
    # Cero quita el descuento.
    percent: Decimal
    reason: str = ""


class ApplyDiscount:
    def __init__(
        self,
        orders: OrderRepository,
        policy: DiscountPolicy,
        activity: ActivityRecorder,
        events: EventPublisher,
    ) -> None:
        self._orders = orders
        self._policy = policy
        self._activity = activity
        self._events = events

    async def __call__(self, command: ApplyDiscountCommand) -> Order:
        actor = command.actor
        order = await find_visible_order(self._orders, actor, command.order_id, for_update=True)
        ensure_owns_or_manages(order, actor, "darle un descuento")
        limit = (
            None
            if can_discount_freely(actor)
            else await self._policy.waiter_limit(actor.restaurant_id)
        )
        order.apply_discount(
            command.percent, command.reason, actor.user_id, datetime.now(UTC), limit
        )
        saved = await self._orders.save(order)
        detail = (
            f"Pedido #{saved.number}: quitó el descuento"
            if saved.discount_percent == 0
            else f"Pedido #{saved.number}: {saved.discount_percent} % "
            f"(S/ {saved.discount_amount}), {saved.discount_reason}"
        )
        await self._activity.record(
            saved.restaurant_id, actor.user_id, ActivityKind.ORDER_DISCOUNTED, detail
        )
        announce(self._events, saved)
        return saved


@dataclass(frozen=True, slots=True)
class CourtesyCommand:
    actor: Principal
    order_id: int
    item_id: int
    # Con motivo se invita el plato; sin él (`None`) se deja de invitar.
    reason: str | None


class SetCourtesy:
    """Invita un plato (la casa no lo cobra) o deshace la invitación."""

    def __init__(
        self, orders: OrderRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._orders = orders
        self._activity = activity
        self._events = events

    async def __call__(self, command: CourtesyCommand) -> Order:
        actor = command.actor
        order = await find_visible_order(self._orders, actor, command.order_id, for_update=True)
        now = datetime.now(UTC)
        if command.reason is None:
            item = order.revoke_courtesy(command.item_id, now)
            detail = f"Pedido #{order.number}: {item.name} vuelve a cobrarse"
        else:
            item = order.grant_courtesy(command.item_id, command.reason, now)
            detail = (
                f"Pedido #{order.number}: invitó {item.quantity} × {item.name} "
                f"(S/ {item.subtotal}), {item.courtesy_reason}"
            )
        saved = await self._orders.save(order)
        await self._activity.record(
            saved.restaurant_id, actor.user_id, ActivityKind.ORDER_COURTESY, detail
        )
        announce(self._events, saved)
        return saved
