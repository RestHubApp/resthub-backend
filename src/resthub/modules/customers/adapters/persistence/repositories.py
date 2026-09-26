from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String, column, func, or_, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page
from resthub.core.timestamps import as_utc
from resthub.modules.customers.adapters.persistence.models import CustomerRow
from resthub.modules.customers.domain.customers import Customer, CustomerStats
from resthub.modules.customers.domain.exceptions import CustomerNotFound
from resthub.modules.customers.ports.customer_repository import CustomerOrder, CustomerQuery


def _customer(row: CustomerRow) -> Customer:
    return Customer(
        id=row.id,
        restaurant_id=row.restaurant_id,
        name=row.name,
        phone=row.phone,
        email=row.email,
        address=row.address,
        reference=row.reference,
        notes=row.notes,
        created_at=as_utc(row.created_at),
    )


def _copy(customer: Customer, row: CustomerRow) -> None:
    row.restaurant_id = customer.restaurant_id
    row.name = customer.name
    row.phone = customer.phone
    row.phone_key = customer.phone_key
    row.email = customer.email
    row.address = customer.address
    row.reference = customer.reference
    row.notes = customer.notes
    row.created_at = customer.created_at


class SqlAlchemyCustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, customer: Customer) -> Customer:
        row = CustomerRow()
        _copy(customer, row)
        self._session.add(row)
        await self._session.flush()
        return _customer(row)

    async def get(self, restaurant_id: int, customer_id: int) -> Customer | None:
        row = await self._row(restaurant_id, customer_id)
        return _customer(row) if row else None

    async def find_by_phone(self, restaurant_id: int, phone_key: str) -> Customer | None:
        row = (
            await self._session.execute(
                select(CustomerRow)
                .where(
                    CustomerRow.restaurant_id == restaurant_id, CustomerRow.phone_key == phone_key
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return _customer(row) if row else None

    async def save(self, customer: Customer) -> Customer:
        row = await self._row(customer.restaurant_id, customer.id or 0)
        if row is None:
            raise CustomerNotFound(customer.id or 0)
        _copy(customer, row)
        await self._session.flush()
        return _customer(row)

    async def search(self, query: CustomerQuery) -> Page[Customer]:
        base = select(CustomerRow).where(CustomerRow.restaurant_id == query.restaurant_id)
        text = query.text.strip()
        if text:
            like = f"%{text.lower()}%"
            base = base.where(
                or_(
                    func.lower(CustomerRow.name).like(like),
                    CustomerRow.phone_key.like(f"%{text.replace(' ', '')}%"),
                )
            )
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        rows = await self._session.scalars(
            base.order_by(CustomerRow.name).limit(query.limit).offset(query.offset)
        )
        return Page(items=[_customer(row) for row in rows], total=total)

    async def _row(self, restaurant_id: int, customer_id: int) -> CustomerRow | None:
        return (
            await self._session.execute(
                select(CustomerRow).where(
                    CustomerRow.id == customer_id, CustomerRow.restaurant_id == restaurant_id
                )
            )
        ).scalar_one_or_none()


# Los pedidos son de `orders`; se leen por SQL y nunca se escriben.
_orders = table(
    "orders",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("customer_id", Integer),
    column("number", Integer),
    column("type", String),
    column("status", String),
    column("total", Numeric(10, 2)),
    column("created_at", DateTime(timezone=True)),
)


class SqlCustomerHistory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stats(
        self, restaurant_id: int, customer_ids: Collection[int]
    ) -> dict[int, CustomerStats]:
        if not customer_ids:
            return {}
        result = await self._session.execute(
            select(
                _orders.c.customer_id,
                func.count(_orders.c.id).label("visits"),
                func.sum(_orders.c.total).label("spent"),
                func.max(_orders.c.created_at).label("last_visit"),
            )
            .where(
                _orders.c.restaurant_id == restaurant_id,
                _orders.c.customer_id.in_(list(customer_ids)),
                _orders.c.status == "paid",
            )
            .group_by(_orders.c.customer_id)
        )
        return {
            int(row.customer_id): CustomerStats(
                visits=int(row.visits),
                spent=Decimal(str(row.spent or 0)).quantize(Decimal("0.01")),
                last_visit=as_utc(row.last_visit) if row.last_visit else None,
            )
            for row in result
        }

    async def orders(self, restaurant_id: int, customer_id: int, limit: int) -> list[CustomerOrder]:
        result = await self._session.execute(
            select(_orders)
            .where(_orders.c.restaurant_id == restaurant_id, _orders.c.customer_id == customer_id)
            .order_by(_orders.c.created_at.desc())
            .limit(limit)
        )
        return [
            CustomerOrder(
                order_id=int(row.id),
                number=int(row.number),
                type=str(row.type),
                status=str(row.status),
                total=Decimal(str(row.total)),
                created_at=as_utc(row.created_at),
            )
            for row in result
        ]
