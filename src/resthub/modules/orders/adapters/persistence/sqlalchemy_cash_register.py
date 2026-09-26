from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page
from resthub.core.timestamps import as_utc
from resthub.modules.orders.adapters.persistence.mappers import (
    cash_session_to_entity,
    copy_cash_session_state,
)
from resthub.modules.orders.adapters.persistence.models import (
    CashSessionRow,
    OrderRow,
    PaymentRow,
)
from resthub.modules.orders.domain.cash import CashOrderAdjustment, CashPayment, CashSession
from resthub.modules.orders.domain.exceptions import CashRegisterAlreadyOpen, CashSessionNotFound
from resthub.modules.orders.domain.orders import PaymentMethod


class SqlAlchemyCashRegister:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def current(self, restaurant_id: int, *, for_update: bool = False) -> CashSession | None:
        statement = select(CashSessionRow).where(
            CashSessionRow.restaurant_id == restaurant_id, CashSessionRow.closed_at.is_(None)
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return cash_session_to_entity(row) if row else None

    async def get(self, restaurant_id: int, session_id: int) -> CashSession | None:
        row = await self._row(restaurant_id, session_id)
        return cash_session_to_entity(row) if row else None

    async def open(self, session: CashSession) -> CashSession:
        row = CashSessionRow()
        copy_cash_session_state(session, row)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Solo lo dispara el índice parcial de una caja abierta por local.
            await self._session.rollback()
            raise CashRegisterAlreadyOpen() from error
        return cash_session_to_entity(row)

    async def save(self, session: CashSession) -> CashSession:
        row = await self._row(session.restaurant_id, session.id or 0)
        if row is None:
            raise CashSessionNotFound(session.id)
        copy_cash_session_state(session, row)
        await self._session.flush()
        return cash_session_to_entity(row)

    async def history(self, restaurant_id: int, limit: int, offset: int) -> Page[CashSession]:
        base = select(CashSessionRow).where(CashSessionRow.restaurant_id == restaurant_id)
        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        rows = await self._session.scalars(
            base.order_by(CashSessionRow.opened_at.desc(), CashSessionRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return Page(items=[cash_session_to_entity(row) for row in rows], total=total)

    async def payments(self, restaurant_id: int, session_id: int) -> list[CashPayment]:
        result = await self._session.execute(
            select(
                PaymentRow.order_id,
                OrderRow.number,
                OrderRow.waiter_id,
                PaymentRow.received_by,
                PaymentRow.method,
                PaymentRow.amount,
                PaymentRow.tip,
                PaymentRow.created_at,
            )
            .join(OrderRow, OrderRow.id == PaymentRow.order_id)
            .where(
                PaymentRow.restaurant_id == restaurant_id,
                PaymentRow.cash_session_id == session_id,
            )
            .order_by(PaymentRow.id)
        )
        return [
            CashPayment(
                order_id=row.order_id,
                order_number=row.number,
                waiter_id=row.waiter_id,
                received_by=row.received_by,
                method=PaymentMethod(row.method),
                amount=row.amount,
                tip=row.tip,
                created_at=as_utc(row.created_at),
            )
            for row in result
        ]

    async def adjustments(self, restaurant_id: int, session_id: int) -> list[CashOrderAdjustment]:
        orders_in_session = (
            select(PaymentRow.order_id)
            .where(
                PaymentRow.restaurant_id == restaurant_id,
                PaymentRow.cash_session_id == session_id,
            )
            .distinct()
        )
        result = await self._session.execute(
            select(OrderRow.id, OrderRow.discount_amount, OrderRow.courtesy_amount).where(
                OrderRow.restaurant_id == restaurant_id, OrderRow.id.in_(orders_in_session)
            )
        )
        return [
            CashOrderAdjustment(
                order_id=row.id,
                discount_amount=row.discount_amount,
                courtesy_amount=row.courtesy_amount,
            )
            for row in result
        ]

    async def _row(self, restaurant_id: int, session_id: int) -> CashSessionRow | None:
        result = await self._session.execute(
            select(CashSessionRow).where(
                CashSessionRow.id == session_id, CashSessionRow.restaurant_id == restaurant_id
            )
        )
        return result.scalar_one_or_none()
