"""Intentos de acceso en la base: el límite vale igual con varias réplicas (S3b)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.login_attempts import (
    LoginAttemptRow,
    LoginGuard,
    SqlLoginAttemptStore,
)
from resthub.core.login_throttle import LoginThrottle
from tests.fakes import FixedClock

EMAIL = "rosa@local-a.pe"
HOME = "200.48.1.7"


@pytest.fixture
def factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def store(factory: async_sessionmaker[AsyncSession]) -> SqlLoginAttemptStore:
    return SqlLoginAttemptStore(factory, FixedClock())


async def test_two_replicas_share_the_count(store: SqlLoginAttemptStore) -> None:
    # Cada réplica tiene su memoria, pero la base es una sola.
    first, second = LoginGuard(LoginThrottle(), store), LoginGuard(LoginThrottle(), store)

    waits = [await (first if n % 2 else second).acquire(EMAIL, HOME) for n in range(6)]

    assert waits[:5] == [0] * 5
    assert waits[5] > 0


async def test_rotating_addresses_hits_the_per_email_cap(store: SqlLoginAttemptStore) -> None:
    waits = [await store.acquire(EMAIL, f"10.0.0.{n}") for n in range(26)]

    assert waits[:25] == [0] * 25
    assert waits[25] > 0


async def test_success_clears_the_pair_but_not_the_email_cap(
    store: SqlLoginAttemptStore,
) -> None:
    store.max_failures_per_email = 6
    for _ in range(4):
        await store.acquire(EMAIL, HOME)
    await store.acquire(EMAIL, HOME)
    async with store.factory() as session, session.begin():
        await store.succeeded(EMAIL, HOME, session)

    # El par quedó limpio: cinco intentos más desde la misma IP...
    assert await store.acquire(EMAIL, HOME) == 0
    assert await store.acquire(EMAIL, HOME) == 0
    # ...pero los cuatro fallos siguen contando para el tope por correo.
    assert await store.acquire(EMAIL, HOME) > 0


async def test_attempts_expire_after_the_window(
    factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    clock = FixedClock()
    store = SqlLoginAttemptStore(factory, clock)
    for _ in range(5):
        await store.acquire(EMAIL, HOME)
    blocked = await store.acquire(EMAIL, HOME)

    clock.advance(minutes=16)
    allowed = await store.acquire(EMAIL, HOME)
    rows = (await session.execute(select(LoginAttemptRow))).scalars().all()

    assert 0 < blocked <= 15 * 60 + 1
    assert allowed == 0
    # Las vencidas se borraron: solo queda el intento recién anotado.
    assert len(rows) == 1


async def test_neither_the_email_nor_the_address_are_stored_in_clear(
    store: SqlLoginAttemptStore, session: AsyncSession
) -> None:
    await store.acquire(EMAIL, HOME)

    row = (await session.execute(select(LoginAttemptRow))).scalar_one()

    assert EMAIL not in (row.email_key, row.address_key)
    assert HOME not in (row.email_key, row.address_key)
    assert len(row.email_key) == len(row.address_key) == 64


async def test_each_scope_counts_its_attempts_apart(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FixedClock()
    staff = SqlLoginAttemptStore(factory, clock)
    platform = SqlLoginAttemptStore(factory, clock, "platform")
    for _ in range(5):
        await staff.acquire(EMAIL, HOME)

    assert await staff.acquire(EMAIL, HOME) > 0
    assert await platform.acquire(EMAIL, HOME) == 0


async def test_attempt_rejected_by_the_shared_count_frees_its_memory_slot(
    store: SqlLoginAttemptStore,
) -> None:
    for _ in range(5):
        await store.acquire(EMAIL, HOME)
    memory = LoginThrottle()
    guard = LoginGuard(memory, store)

    assert await guard.acquire(EMAIL, HOME) > 0
    assert memory.retry_after(EMAIL, HOME) == 0
    assert memory._failures == {}
    assert memory._by_email == {}
