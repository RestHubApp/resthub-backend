"""Emitir un comprobante y anular el cobro de su pedido no se cruzan (solo PostgreSQL).

SQLite no tiene bloqueo de filas: allí `FOR UPDATE` no hace nada y estas
pruebas se saltan. Cada lado usa su propia sesión, como dos peticiones.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.modules.billing.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyPaidOrderDirectory,
)
from resthub.modules.orders.adapters.persistence.sqlalchemy_order_repository import (
    SqlAlchemyOrderRepository,
)
from resthub.modules.orders.domain.orders import Order
from tests.builders import seed_menu
from tests.conftest import DATABASE_UNDER_TEST, StaffedRestaurant
from tests.fakes_lycet import paid_order
from tests.test_api_cash import _open_cash_register

pytestmark = pytest.mark.skipif(
    not DATABASE_UNDER_TEST.is_postgres, reason="SQLite no bloquea filas"
)

# Lo que se espera para dar por hecho que el otro lado quedó bloqueado.
BLOCKED_FOR = 0.3


@pytest.fixture
async def seeded_order(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> int:
    menu = await seed_menu(session, restaurant_a.id)
    await _open_cash_register(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu)
    await session.commit()
    return int(order["id"])


@pytest.fixture
def sessions(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)


def _void_payment(order: Order, actor_id: int) -> None:
    payment_id = order.payments[0].id or 0
    order.void_payment(payment_id, "Se marcó Yape y pagó en efectivo", actor_id, order.updated_at)


async def test_issue_invoice_waits_for_payment_void_and_sees_order_unpaid(
    seeded_order: int,
    restaurant_a: StaffedRestaurant,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as voiding, sessions() as issuing:
        orders = SqlAlchemyOrderRepository(voiding)
        order = await orders.get(restaurant_a.id, seeded_order, for_update=True)
        assert order is not None
        _void_payment(order, restaurant_a.admin.id or 0)
        await orders.save(order)

        lookup = asyncio.create_task(
            SqlAlchemyPaidOrderDirectory(issuing).get(
                restaurant_a.id, seeded_order, for_update=True
            )
        )
        done, _ = await asyncio.wait({lookup}, timeout=BLOCKED_FOR)
        assert not done
        await voiding.commit()

        assert await lookup is None
        await issuing.rollback()


async def test_payment_void_waits_for_an_issuance_in_progress(
    seeded_order: int,
    restaurant_a: StaffedRestaurant,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as issuing, sessions() as voiding:
        paid = await SqlAlchemyPaidOrderDirectory(issuing).get(
            restaurant_a.id, seeded_order, for_update=True
        )
        assert paid is not None

        lock = asyncio.create_task(
            SqlAlchemyOrderRepository(voiding).get(restaurant_a.id, seeded_order, for_update=True)
        )
        done, _ = await asyncio.wait({lock}, timeout=BLOCKED_FOR)
        assert not done
        await issuing.commit()

        assert await lock is not None
        await voiding.rollback()
