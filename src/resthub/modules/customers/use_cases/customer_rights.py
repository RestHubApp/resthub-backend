"""Derechos ARCO del cliente (Ley N.º 29733): acceso y cancelación.

- Acceso: todo lo que el local guarda de él, en un solo documento.
- Rectificación: es la edición de siempre (`SaveCustomer`).
- Cancelación y oposición: se borran sus datos de la ficha, de sus pedidos y de
  sus reservas. La ficha queda, anónima, para que las ventas sigan cuadrando.
  Los comprobantes electrónicos no se tocan: la ley tributaria obliga a
  conservarlos.

Cada uno queda en la bitácora, sin el nombre: el registro tiene que sobrevivir
al borrado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.modules.customers.domain.customers import ANONYMIZED_NAME, Customer
from resthub.modules.customers.domain.exceptions import CustomerHasActiveOrders
from resthub.modules.customers.ports.customer_repository import CustomerRepository
from resthub.modules.customers.ports.linked_records import (
    LinkedOrder,
    LinkedRecords,
    LinkedReservation,
)
from resthub.modules.customers.use_cases.manage_customers import find_customer


@dataclass(frozen=True, slots=True)
class CustomerExport:
    customer: Customer
    orders: tuple[LinkedOrder, ...]
    reservations: tuple[LinkedReservation, ...]
    exported_at: datetime


class ExportCustomerData:
    def __init__(
        self, customers: CustomerRepository, linked: LinkedRecords, activity: ActivityRecorder
    ) -> None:
        self._customers = customers
        self._linked = linked
        self._activity = activity

    async def __call__(self, restaurant_id: int, actor_id: int, customer_id: int) -> CustomerExport:
        customer = await find_customer(self._customers, restaurant_id, customer_id)
        export = CustomerExport(
            customer=customer,
            orders=tuple(await self._linked.orders(restaurant_id, customer_id)),
            reservations=tuple(await self._linked.reservations(restaurant_id, customer_id)),
            exported_at=datetime.now(UTC),
        )
        await self._activity.record(
            restaurant_id, actor_id, ActivityKind.CUSTOMER_EXPORTED, f"Cliente #{customer_id}"
        )
        return export


class AnonymizeCustomer:
    def __init__(
        self, customers: CustomerRepository, linked: LinkedRecords, activity: ActivityRecorder
    ) -> None:
        self._customers = customers
        self._linked = linked
        self._activity = activity

    async def __call__(self, restaurant_id: int, actor_id: int, customer_id: int) -> None:
        customer = await find_customer(self._customers, restaurant_id, customer_id)
        # Borrar las notas de un pedido que la cocina todavía prepara le quita
        # a la cocina lo que necesita saber (una alergia, por ejemplo).
        if await self._linked.has_active_orders(restaurant_id, customer_id):
            raise CustomerHasActiveOrders()
        customer.anonymize(datetime.now(UTC))
        await self._customers.save(customer)
        await self._linked.anonymize(restaurant_id, customer_id, ANONYMIZED_NAME)
        await self._activity.record(
            restaurant_id, actor_id, ActivityKind.CUSTOMER_ANONYMIZED, f"Cliente #{customer_id}"
        )
