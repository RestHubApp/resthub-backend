"""Prevención de fugas internas: bitácora de acceso a fichas y alerta de consulta masiva.

Abrir la ficha de un cliente, exportarla o pedir sus datos completos queda en
la bitácora con quién y cuándo. Si una misma cuenta abre o exporta más de `N`
fichas en `M` minutos, se registra una alerta y se avisa a quien puede ver la
bitácora. Sin servicios externos: todo vive en la bitácora y en los avisos en
tiempo real del propio sistema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from resthub.core.activity import (
    ActivityKind,
    ActivityQuery,
    ActivityReader,
    ActivityRecorder,
)
from resthub.core.clock import Clock
from resthub.core.permissions import Permission
from resthub.core.realtime import DATA_ACCESS_ALERT_TOPIC, EventPublisher, RealtimeEvent
from resthub.modules.customers.ports.customer_repository import (
    CustomerHistoryReader,
    CustomerRepository,
)
from resthub.modules.customers.use_cases.manage_customers import (
    CustomerCard,
    ReadCustomerCard,
    find_customer,
)

# Lo que cuenta como «abrir una ficha» para la alerta. Pedir el dato completo
# no suma: siempre viene después de abrir la ficha y contaría dos veces.
_COUNTED_KINDS = frozenset({ActivityKind.CUSTOMER_VIEWED, ActivityKind.CUSTOMER_EXPORTED})


@dataclass(frozen=True, slots=True)
class DataAccessPolicy:
    """Más de `max_cards` fichas en `window_minutes` minutos dispara la alerta."""

    max_cards: int = 20
    window_minutes: int = 10


class DataAccessMonitor:
    def __init__(
        self,
        recorder: ActivityRecorder,
        reader: ActivityReader,
        events: EventPublisher,
        clock: Clock,
        policy: DataAccessPolicy,
    ) -> None:
        self._recorder = recorder
        self._reader = reader
        self._events = events
        self._clock = clock
        self._policy = policy

    async def record(
        self, restaurant_id: int, actor_id: int, kind: ActivityKind, customer_id: int
    ) -> None:
        """Anota el acceso y, si es de los que cuentan, revisa si ya son demasiados."""
        await self._recorder.record(restaurant_id, actor_id, kind, f"Cliente #{customer_id}")
        if kind in _COUNTED_KINDS:
            await self._alert_if_excessive(restaurant_id, actor_id)

    async def _alert_if_excessive(self, restaurant_id: int, actor_id: int) -> None:
        since = self._clock.now() - timedelta(minutes=self._policy.window_minutes)
        opened = await self._reader.search(
            ActivityQuery(
                restaurant_id=restaurant_id,
                user_ids=frozenset({actor_id}),
                kinds=_COUNTED_KINDS,
                since=since,
                limit=1,
            )
        )
        if opened.total <= self._policy.max_cards:
            return
        # Una alerta por ventana y cuenta: no se repite con cada ficha de más.
        already = await self._reader.search(
            ActivityQuery(
                restaurant_id=restaurant_id,
                user_ids=frozenset({actor_id}),
                kinds=frozenset({ActivityKind.DATA_ACCESS_ALERT}),
                since=since,
                limit=1,
            )
        )
        if already.total:
            return
        await self._recorder.record(
            restaurant_id,
            actor_id,
            ActivityKind.DATA_ACCESS_ALERT,
            f"{opened.total} fichas en {self._policy.window_minutes} min",
        )
        self._events.publish(
            RealtimeEvent(
                restaurant_id=restaurant_id,
                topic=DATA_ACCESS_ALERT_TOPIC,
                permissions=frozenset({Permission.ACTIVITY_READ.value}),
                reference_id=actor_id,
            )
        )


class OpenCustomerCard:
    """Abrir la ficha de un cliente: la lee y deja el acceso en la bitácora."""

    def __init__(
        self,
        customers: CustomerRepository,
        history: CustomerHistoryReader,
        monitor: DataAccessMonitor,
    ) -> None:
        self._read = ReadCustomerCard(customers, history)
        self._monitor = monitor

    async def __call__(self, restaurant_id: int, actor_id: int, customer_id: int) -> CustomerCard:
        card = await self._read(restaurant_id, customer_id)
        await self._monitor.record(
            restaurant_id, actor_id, ActivityKind.CUSTOMER_VIEWED, customer_id
        )
        return card


@dataclass(frozen=True, slots=True)
class CustomerContact:
    """Los datos de contacto completos de un cliente, sin enmascarar."""

    phone: str
    email: str


class RevealCustomerContact:
    """«Ver completo»: devuelve el teléfono y el correo y lo deja en la bitácora."""

    def __init__(self, customers: CustomerRepository, monitor: DataAccessMonitor) -> None:
        self._customers = customers
        self._monitor = monitor

    async def __call__(
        self, restaurant_id: int, actor_id: int, customer_id: int
    ) -> CustomerContact:
        customer = await find_customer(self._customers, restaurant_id, customer_id)
        await self._monitor.record(
            restaurant_id, actor_id, ActivityKind.CUSTOMER_REVEALED, customer_id
        )
        return CustomerContact(phone=customer.phone, email=customer.email)
