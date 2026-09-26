"""Dar de alta, editar, buscar y ver la ficha de un cliente.

Exige `customers.read` para buscar y ver (el mesero lo busca al tomar un
delivery) y `customers.manage` para dar de alta y editar.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.pagination import Page
from resthub.modules.customers.domain.customers import Customer, CustomerStats
from resthub.modules.customers.domain.exceptions import CustomerNotFound, PhoneTaken
from resthub.modules.customers.ports.customer_repository import (
    CustomerHistory,
    CustomerOrder,
    CustomerQuery,
    CustomerRepository,
)

RECENT_ORDERS = 10


@dataclass(frozen=True, slots=True)
class CustomerData:
    name: str
    phone: str = ""
    email: str = ""
    address: str = ""
    reference: str = ""
    notes: str = ""


class SaveCustomer:
    """Alta (sin `customer_id`) o edición. El teléfono no se repite en el local."""

    def __init__(self, customers: CustomerRepository, activity: ActivityRecorder) -> None:
        self._customers = customers
        self._activity = activity

    async def __call__(
        self, restaurant_id: int, actor_id: int, data: CustomerData, customer_id: int | None = None
    ) -> Customer:
        candidate = Customer(
            restaurant_id=restaurant_id,
            name=data.name,
            phone=data.phone,
            email=data.email,
            address=data.address,
            reference=data.reference,
            notes=data.notes,
        )
        if candidate.phone:
            owner = await self._customers.find_by_phone(restaurant_id, candidate.phone_key)
            if owner is not None and owner.id != customer_id:
                raise PhoneTaken(candidate.phone, owner.name)
        if customer_id is None:
            saved = await self._customers.add(candidate)
            kind = ActivityKind.CUSTOMER_CREATED
        else:
            current = await find_customer(self._customers, restaurant_id, customer_id)
            candidate.id, candidate.created_at = current.id, current.created_at
            saved = await self._customers.save(candidate)
            kind = ActivityKind.CUSTOMER_UPDATED
        await self._activity.record(restaurant_id, actor_id, kind, saved.name)
        return saved


async def find_customer(
    customers: CustomerRepository, restaurant_id: int, customer_id: int
) -> Customer:
    customer = await customers.get(restaurant_id, customer_id)
    if customer is None:
        raise CustomerNotFound(customer_id)
    return customer


@dataclass(frozen=True, slots=True)
class CustomerCard:
    customer: Customer
    stats: CustomerStats
    recent_orders: tuple[CustomerOrder, ...] = ()


class SearchCustomers:
    def __init__(self, customers: CustomerRepository, history: CustomerHistory) -> None:
        self._customers = customers
        self._history = history

    async def __call__(self, query: CustomerQuery) -> Page[CustomerCard]:
        page = await self._customers.search(query)
        stats = await self._history.stats(
            query.restaurant_id, {customer.id or 0 for customer in page.items}
        )
        return Page(
            items=[
                CustomerCard(customer=c, stats=stats.get(c.id or 0, CustomerStats()))
                for c in page.items
            ],
            total=page.total,
        )


class ReadCustomerCard:
    def __init__(self, customers: CustomerRepository, history: CustomerHistory) -> None:
        self._customers = customers
        self._history = history

    async def __call__(self, restaurant_id: int, customer_id: int) -> CustomerCard:
        customer = await find_customer(self._customers, restaurant_id, customer_id)
        stats = await self._history.stats(restaurant_id, {customer_id})
        return CustomerCard(
            customer=customer,
            stats=stats.get(customer_id, CustomerStats()),
            recent_orders=tuple(
                await self._history.orders(restaurant_id, customer_id, RECENT_ORDERS)
            ),
        )
