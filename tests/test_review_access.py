"""Revisión de accesos: las cuentas activas sin uso en 60 días (RFS-24)."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import (
    VALID_PASSWORD,
    FakeClock,
    StaffedRestaurant,
    authorization_for,
    url,
)

UNUSED_URL = url("staff/unused")
SIXTY_ONE_DAYS = 61 * 24 * 3600


async def _unused(
    client: AsyncClient, restaurant: StaffedRestaurant, **params: int
) -> list[dict[str, Any]]:
    response = await client.get(
        UNUSED_URL, params=params, headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 200, response.text
    return list(response.json())


async def test_a_new_account_is_not_unused_yet(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    assert await _unused(client, restaurant_a) == []


async def test_an_account_that_never_signed_in_counts_from_its_creation(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    clock.advance(SIXTY_ONE_DAYS)

    unused = await _unused(client, restaurant_a)

    # El encargado acaba de usar el sistema para preguntar: no figura.
    assert [entry["member"]["id"] for entry in unused] == [restaurant_a.waiter.id]
    assert unused[0]["last_activity_at"] is None
    assert unused[0]["unused_days"] == 61


async def test_the_last_use_of_a_session_counts(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    login = await client.post(
        url("auth/login"), json={"email": "mesero@local-a.pe", "password": VALID_PASSWORD}
    )
    assert login.status_code == 200
    clock.advance(30 * 24 * 3600)

    recent = await _unused(client, restaurant_a)
    clock.advance(SIXTY_ONE_DAYS - 30 * 24 * 3600)
    old = await _unused(client, restaurant_a)
    wider = await _unused(client, restaurant_a, days=90)

    assert recent == []
    assert [entry["member"]["id"] for entry in old] == [restaurant_a.waiter.id]
    assert old[0]["last_activity_at"] is not None
    assert wider == []


async def test_deactivated_accounts_are_left_out(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    deactivated = await client.patch(
        url(f"staff/{restaurant_a.waiter.id}/status"),
        json={"is_active": False, "reason": "Ya no trabaja en el local"},
        headers=authorization_for(restaurant_a.admin),
    )
    assert deactivated.status_code == 200
    clock.advance(SIXTY_ONE_DAYS)

    assert await _unused(client, restaurant_a) == []


async def test_only_the_restaurant_and_with_permission(
    client: AsyncClient,
    clock: FakeClock,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    clock.advance(SIXTY_ONE_DAYS)

    unused = await _unused(client, restaurant_a)
    waiter = await client.get(UNUSED_URL, headers=authorization_for(restaurant_a.waiter))

    ids = {entry["member"]["id"] for entry in unused}
    assert restaurant_b.waiter.id not in ids
    assert restaurant_b.admin.id not in ids
    assert waiter.status_code == 403
