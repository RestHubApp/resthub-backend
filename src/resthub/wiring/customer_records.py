"""Clientes → sus pedidos y reservas, para los derechos ARCO (Ley N.º 29733).

`customers` declara el puerto `LinkedRecords` sin saber cómo guardan `orders` y
`reservations` lo que anotaron de un cliente. Este adaptador lo lee y lo borra
con sus tablas, en la sesión de la petición: borrar la ficha, los pedidos y las
reservas es una sola transacción. `main.py` lo instala.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.auth import SessionDep
from resthub.core.timestamps import as_utc
from resthub.modules.customers.ports.linked_records import (
    LinkedOrder,
    LinkedRecords,
    LinkedReservation,
)
from resthub.modules.orders.adapters.persistence.models import OrderItemRow, OrderRow
from resthub.modules.orders.domain.orders import OrderStatus
from resthub.modules.reservations.adapters.persistence.models import ReservationRow

_CLOSED = (OrderStatus.PAID.value, OrderStatus.CANCELLED.value)


def _orders_of(restaurant_id: int, customer_id: int) -> tuple[ColumnElement[bool], ...]:
    return (OrderRow.restaurant_id == restaurant_id, OrderRow.customer_id == customer_id)


class SqlLinkedRecords:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_active_orders(self, restaurant_id: int, customer_id: int) -> bool:
        found = await self._session.execute(
            select(OrderRow.id)
            .where(*_orders_of(restaurant_id, customer_id), OrderRow.status.not_in(_CLOSED))
            .limit(1)
        )
        return found.first() is not None

    async def orders(self, restaurant_id: int, customer_id: int) -> list[LinkedOrder]:
        rows = await self._session.scalars(
            select(OrderRow)
            .where(*_orders_of(restaurant_id, customer_id))
            .order_by(OrderRow.created_at)
        )
        return [
            LinkedOrder(
                number=row.number,
                type=str(row.type),
                status=str(row.status),
                total=Decimal(str(row.total)),
                created_at=as_utc(row.created_at),
                customer_name=row.customer_name,
                customer_phone=row.customer_phone,
                delivery_address=row.delivery_address,
                delivery_reference=row.delivery_reference,
                notes=row.notes,
            )
            for row in rows
        ]

    async def reservations(self, restaurant_id: int, customer_id: int) -> list[LinkedReservation]:
        rows = await self._session.scalars(
            select(ReservationRow)
            .where(
                ReservationRow.restaurant_id == restaurant_id,
                ReservationRow.customer_id == customer_id,
            )
            .order_by(ReservationRow.reserved_for)
        )
        return [
            LinkedReservation(
                reserved_for=as_utc(row.reserved_for),
                party_size=row.party_size,
                status=str(row.status),
                customer_name=row.customer_name,
                phone=row.phone,
                notes=row.notes,
            )
            for row in rows
        ]

    async def anonymize(self, restaurant_id: int, customer_id: int, name: str) -> None:
        orders = _orders_of(restaurant_id, customer_id)
        await self._session.execute(
            update(OrderItemRow)
            .where(
                OrderItemRow.restaurant_id == restaurant_id,
                OrderItemRow.order_id.in_(select(OrderRow.id).where(*orders)),
            )
            .values(notes="")
        )
        await self._session.execute(
            update(OrderRow)
            .where(*orders)
            .values(
                customer_name=name,
                customer_phone="",
                delivery_address="",
                delivery_reference="",
                notes="",
            )
        )
        await self._session.execute(
            update(ReservationRow)
            .where(
                ReservationRow.restaurant_id == restaurant_id,
                ReservationRow.customer_id == customer_id,
            )
            .values(customer_name=name, phone="", notes="")
        )


def get_customer_linked_records(session: SessionDep) -> LinkedRecords:
    return SqlLinkedRecords(session)
