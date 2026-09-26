from __future__ import annotations

from datetime import date

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page
from resthub.modules.orders.adapters.persistence.mappers import (
    copy_order_state,
    item_to_row,
    order_to_entity,
    order_to_row,
    payment_to_row,
)
from resthub.modules.orders.adapters.persistence.models import OrderItemRow, OrderRow
from resthub.modules.orders.domain.exceptions import OrderNotFound, OrderNumberTaken
from resthub.modules.orders.domain.orders import ACTIVE_STATUSES, Order
from resthub.modules.orders.ports.order_repository import OrderQuery

_ACTIVE = [status.value for status in ACTIVE_STATUSES]


class SqlAlchemyOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, order: Order) -> Order:
        row = order_to_row(order)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Solo lo dispara el índice único del correlativo diario.
            await self._session.rollback()
            raise OrderNumberTaken() from error
        # Sin `refresh`: el identificador ya llegó con el `flush`, y volver a
        # leer la fila no aporta nada que el dominio no tenga.
        return order_to_entity(row)

    async def by_client_request(self, restaurant_id: int, client_request_id: str) -> Order | None:
        row = (
            await self._session.execute(
                select(OrderRow).where(
                    OrderRow.restaurant_id == restaurant_id,
                    OrderRow.client_request_id == client_request_id,
                )
            )
        ).scalar_one_or_none()
        return order_to_entity(row) if row else None

    async def get(
        self, restaurant_id: int, order_id: int, *, for_update: bool = False
    ) -> Order | None:
        row = await self._row(restaurant_id, order_id, for_update=for_update)
        return order_to_entity(row) if row else None

    async def save(self, order: Order) -> Order:
        row = await self._row(order.restaurant_id, order.id or 0)
        if row is None:
            raise OrderNotFound(order.id or 0)
        copy_order_state(order, row)

        # Los ítems se sincronizan por identificador: los que el dominio quitó
        # se borran, los que cambió se actualizan y los nuevos se insertan.
        wanted = {item.id: item for item in order.items if item.id is not None}
        for item_row in list(row.items):
            if item_row.id not in wanted:
                row.items.remove(item_row)
                continue
            item = wanted[item_row.id]
            item_row.quantity = item.quantity
            item_row.notes = item.notes
            item_row.is_courtesy = item.is_courtesy
            item_row.courtesy_reason = item.courtesy_reason
        for item in order.items:
            if item.id is None:
                row.items.append(item_to_row(item, order.restaurant_id))

        # Los pagos no se editan ni se borran: solo entran los nuevos.
        new_payments = [
            (payment, payment_to_row(payment, order.restaurant_id))
            for payment in order.payments
            if payment.id is None
        ]
        row.payments.extend(payment_row for _, payment_row in new_payments)
        await self._session.flush()
        if any(payment.item_ids for payment, _ in new_payments):
            by_id = {item_row.id: item_row for item_row in row.items}
            for payment, payment_row in new_payments:
                for item_id in payment.item_ids:
                    by_id[item_id].payment_id = payment_row.id
            await self._session.flush()
        return order_to_entity(row)

    async def save_merge(self, target: Order, source: Order) -> Order:
        target_row = await self._row(target.restaurant_id, target.id or 0)
        source_row = await self._row(source.restaurant_id, source.id or 0)
        if target_row is None or source_row is None:
            raise OrderNotFound(target.id or source.id or 0)
        # Los ítems se mueven de pedido tal cual, con su identificador: el
        # consumo de insumos es idempotente por ítem, y un plato ya servido y
        # descontado no se vuelve a descontar al servir la mesa unida.
        await self._session.execute(
            update(OrderItemRow)
            .where(
                OrderItemRow.restaurant_id == source.restaurant_id,
                OrderItemRow.order_id == source.id,
            )
            .values(order_id=target.id)
        )
        # La colección en memoria quedó vieja: sin olvidarla, la cascada
        # borraría los ítems movidos al guardar el pedido que se cerró.
        self._session.expire(target_row, ["items"])
        self._session.expire(source_row, ["items"])
        copy_order_state(target, target_row)
        copy_order_state(source, source_row)
        await self._session.flush()
        reloaded = await self._row(target.restaurant_id, target.id or 0, for_update=True)
        if reloaded is None:
            raise OrderNotFound(target.id or 0)
        return order_to_entity(reloaded)

    async def search(self, query: OrderQuery) -> Page[Order]:
        base = self._apply_filters(select(OrderRow), query)
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        result = await self._session.execute(
            base.order_by(OrderRow.created_at.desc(), OrderRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        return Page(items=[order_to_entity(row) for row in result.scalars().all()], total=total)

    async def list_active(self, restaurant_id: int) -> list[Order]:
        result = await self._session.execute(
            select(OrderRow)
            .where(OrderRow.restaurant_id == restaurant_id, OrderRow.status.in_(_ACTIVE))
            .order_by(OrderRow.created_at, OrderRow.id)
        )
        return [order_to_entity(row) for row in result.scalars().all()]

    async def active_for_table(self, restaurant_id: int, table_id: int) -> Order | None:
        result = await self._session.execute(
            select(OrderRow)
            .where(
                OrderRow.restaurant_id == restaurant_id,
                OrderRow.table_id == table_id,
                OrderRow.status.in_(_ACTIVE),
            )
            .order_by(OrderRow.id)
        )
        row = result.scalars().first()
        return order_to_entity(row) if row else None

    async def last_number(self, restaurant_id: int, business_date: date) -> int:
        result = await self._session.execute(
            select(func.max(OrderRow.number)).where(
                OrderRow.restaurant_id == restaurant_id,
                OrderRow.business_date == business_date,
            )
        )
        return int(result.scalar_one() or 0)

    async def _row(
        self, restaurant_id: int, order_id: int, *, for_update: bool = False
    ) -> OrderRow | None:
        statement = select(OrderRow).where(
            OrderRow.id == order_id, OrderRow.restaurant_id == restaurant_id
        )
        if for_update:
            # La fila queda tomada hasta el fin de la transacción: otro cobro,
            # un plato agregado o un doble toque sobre el mismo pedido esperan
            # su turno y leen el estado ya guardado. `populate_existing`
            # descarta lo que la sesión tuviera en memoria. SQLite no entiende
            # `FOR UPDATE` y lo omite; ahí las escrituras ya van de a una.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    def _apply_filters(
        self, statement: Select[tuple[OrderRow]], query: OrderQuery
    ) -> Select[tuple[OrderRow]]:
        statement = statement.where(OrderRow.restaurant_id == query.restaurant_id)
        if query.statuses:
            statement = statement.where(
                OrderRow.status.in_([status.value for status in query.statuses])
            )
        if query.date_from is not None:
            statement = statement.where(OrderRow.business_date >= query.date_from)
        if query.date_to is not None:
            statement = statement.where(OrderRow.business_date <= query.date_to)
        if query.type is not None:
            statement = statement.where(OrderRow.type == query.type.value)
        if query.table_id is not None:
            statement = statement.where(OrderRow.table_id == query.table_id)
        if query.waiter_id is not None:
            statement = statement.where(OrderRow.waiter_id == query.waiter_id)
        if query.visible_to is not None:
            statement = statement.where(
                or_(OrderRow.waiter_id == query.visible_to, OrderRow.status.in_(_ACTIVE))
            )
        return statement
