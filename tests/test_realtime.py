"""Avisos en tiempo real: a quién llegan y cuándo salen."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.identity import Principal
from resthub.core.realtime import PERMISSIONS_TOPIC, RealtimeEvent
from resthub.core.realtime_broker import (
    LocalBroker,
    SessionEventPublisher,
    decode_event,
    encode_event,
)
from tests.conftest import StaffedRestaurant, authorization_for

EVENTS_URL = "/api/v1/events"


def _event() -> RealtimeEvent:
    return RealtimeEvent(
        restaurant_id=1,
        topic="orders",
        user_ids=frozenset({3, 7}),
        permissions=frozenset({"insights.read"}),
        reference_id=42,
    )


def _principal(user_id: int, *permissions: str, restaurant_id: int = 1) -> Principal:
    return Principal(
        user_id=user_id,
        role_id=1,
        is_active=True,
        restaurant_id=restaurant_id,
        permissions=frozenset(permissions),
    )


def test_event_reaches_participants_and_permission_holders_only() -> None:
    event = _event()

    assert event.is_for(_principal(3, "orders.take"))
    assert event.is_for(_principal(99, "insights.read", "menu.read"))
    assert not event.is_for(_principal(99, "orders.take", "orders.manage"))


def test_an_event_for_everyone_reaches_any_role_of_the_restaurant() -> None:
    event = RealtimeEvent(restaurant_id=1, topic="menu", everyone=True)

    assert event.is_for(_principal(5))
    assert event.is_for(_principal(6, "orders.manage"))
    assert not event.is_for(_principal(5, restaurant_id=2))


def test_event_never_crosses_to_another_restaurant() -> None:
    event = _event()

    assert not event.is_for(_principal(99, "insights.read", restaurant_id=2))
    assert not event.is_for(_principal(3, restaurant_id=2))


def test_event_survives_the_trip_through_postgres() -> None:
    assert decode_event(encode_event(_event())) == _event()
    everyone = RealtimeEvent(restaurant_id=1, topic="orders", everyone=True, reference_id=1)
    assert decode_event(encode_event(everyone)) == everyone


def test_an_event_from_a_process_before_the_roles_change_still_arrives() -> None:
    """Durante el despliegue conviven procesos que publican con `roles`."""
    viejo = (
        '{"restaurant_id": 1, "topic": "orders", "user_ids": [3], '
        '"roles": ["admin", "waiter"], "reference_id": 8}'
    )

    event = decode_event(viejo)

    assert event.everyone
    assert event.is_for(_principal(50))


async def test_event_is_released_only_after_commit(session: AsyncSession) -> None:
    broker = LocalBroker()
    publisher = SessionEventPublisher(session, broker)

    async with broker.subscribe() as queue:
        publisher.publish(_event())
        assert queue.empty()

        await session.commit()

        assert queue.get_nowait() == _event()


async def test_event_is_discarded_when_the_transaction_rolls_back(session: AsyncSession) -> None:
    broker = LocalBroker()
    publisher = SessionEventPublisher(session, broker)

    async with broker.subscribe() as queue:
        publisher.publish(_event())
        await session.rollback()
        await session.commit()

        assert queue.empty()


async def test_changing_a_role_tells_that_account_to_reload_its_permissions(
    client: AsyncClient, local_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    async with broker.subscribe() as queue:
        response = await client.patch(
            f"/api/v1/staff/{local_a.waiter.id}",
            json={"role_id": local_a.admin.role.id},
            headers=authorization_for(local_a.admin),
        )
        await asyncio.sleep(0)

        assert response.status_code == 200
        received = queue.get_nowait()
    assert received.topic == PERMISSIONS_TOPIC
    assert received.restaurant_id == local_a.id
    assert received.user_ids == {local_a.waiter.id}


async def test_renaming_without_changing_the_role_sends_nothing(
    client: AsyncClient, local_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    async with broker.subscribe() as queue:
        response = await client.patch(
            f"/api/v1/staff/{local_a.waiter.id}",
            json={"full_name": "Luis Alberto Torres"},
            headers=authorization_for(local_a.admin),
        )
        await asyncio.sleep(0)

        assert response.status_code == 200
        assert queue.empty()


async def test_the_event_stream_requires_credentials(client: AsyncClient) -> None:
    response = await client.get(EVENTS_URL)

    assert response.status_code == 401
