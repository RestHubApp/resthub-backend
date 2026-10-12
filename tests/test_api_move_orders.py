"""Cambiar un pedido de mesa y unir dos mesas por HTTP."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.orders.adapters.persistence.sqlalchemy_table_repository import (
    SqlAlchemyTableRepository,
)
from resthub.modules.orders.domain.tables import DiningTable
from tests.builders import SeededMenu, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, build_user, url

ORDERS_URL = url("orders")


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_a.id)


async def _order_at_table(
    client: AsyncClient,
    headers: dict[str, str],
    table_id: int,
    *items: tuple[int, int],
    **extra: Any,
) -> dict[str, Any]:
    response = await client.post(
        ORDERS_URL,
        json={
            "type": "dine_in",
            "table_id": table_id,
            "items": [{"menu_item_id": dish, "quantity": q} for dish, q in items],
            **extra,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _step(
    client: AsyncClient, headers: dict[str, str], order_id: int, step: str
) -> dict[str, Any]:
    response = await client.post(f"{ORDERS_URL}/{order_id}/{step}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_order_moves_to_a_free_table(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    order = await _order_at_table(client, waiter, menu_a.table_1, (menu_a.lomo, 1))

    moved = await client.post(
        f"{ORDERS_URL}/{order['id']}/move", json={"table_id": menu_a.table_2}, headers=waiter
    )
    tables = await client.get(url("tables"), headers=waiter)
    activity_log = await client.get(
        url("activity"),
        params={"kind": ActivityKind.ORDER_MOVED.value},
        headers=authorization_for(restaurant_a.admin),
    )

    assert moved.status_code == 200
    assert moved.json()["table_label"] == "2"
    statuses = {table["label"]: table["status"] for table in tables.json()}
    assert statuses == {"1": "free", "2": "occupied"}
    assert activity_log.json()["items"][0]["detail"] == f"Pedido #{order['number']}: de 1 a 2"


async def test_order_does_not_move_to_an_occupied_or_inactive_table(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    inactive = await SqlAlchemyTableRepository(session).add(
        DiningTable(restaurant_id=restaurant_a.id, label="Terraza", is_active=False)
    )
    await session.commit()
    first = await _order_at_table(client, waiter, menu_a.table_1, (menu_a.lomo, 1))
    await _order_at_table(client, waiter, menu_a.table_2, (menu_a.aji, 1))
    url = f"{ORDERS_URL}/{first['id']}/move"

    occupied = await client.post(url, json={"table_id": menu_a.table_2}, headers=waiter)
    disabled = await client.post(url, json={"table_id": inactive.id}, headers=waiter)

    assert occupied.status_code == 409
    assert disabled.status_code == 409


async def test_merge_tables_joins_dishes_and_frees_the_other(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter, owner = authorization_for(restaurant_a.waiter), authorization_for(restaurant_a.admin)
    await open_cash_session_http(client, restaurant_a)
    first = await _order_at_table(client, waiter, menu_a.table_1, (menu_a.lomo, 1))
    second = await _order_at_table(client, waiter, menu_a.table_2, (menu_a.aji, 2), notes="Sin ají")
    # La mesa 1 ya comió: está servida. La 2 sigue abierta.
    for step, actor in (("send", waiter), ("ready", owner), ("served", waiter)):
        await _step(client, actor, first["id"], step)

    merged = await client.post(
        f"{ORDERS_URL}/{first['id']}/merge", json={"source_order_id": second["id"]}, headers=waiter
    )
    closed_order = await client.get(f"{ORDERS_URL}/{second['id']}", headers=owner)
    tables = await client.get(url("tables"), headers=waiter)
    summary = await client.get(url("insights/summary"), headers=owner)

    body = merged.json()
    assert merged.status_code == 200
    assert body["total"] == "72.00"
    assert body["item_count"] == 3
    # Quedó abierto: los platos de la mesa 2 todavía no fueron a cocina.
    assert body["status"] == "open"
    assert body["notes"] == "Sin ají"
    assert closed_order.json()["status"] == "cancelled"
    assert closed_order.json()["merged_into_id"] == first["id"]
    assert closed_order.json()["cancel_reason"] == f"Unido al pedido #{first['number']}"
    statuses = {table["label"]: table["status"] for table in tables.json()}
    assert statuses == {"1": "occupied", "2": "free"}
    # Una mesa unida no cuenta como pedido cancelado en el panel.
    assert summary.json()["cancelled_orders"] == 0


async def test_tables_with_payments_are_not_merged(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter, owner = authorization_for(restaurant_a.waiter), authorization_for(restaurant_a.admin)
    await open_cash_session_http(client, restaurant_a)
    first = await _order_at_table(client, waiter, menu_a.table_1, (menu_a.lomo, 2))
    second = await _order_at_table(client, waiter, menu_a.table_2, (menu_a.aji, 1))
    for step, actor in (("send", waiter), ("ready", owner), ("served", waiter)):
        await _step(client, actor, first["id"], step)
    partial_payment = await client.post(
        f"{ORDERS_URL}/{first['id']}/payments",
        json={"payment_method": "yape", "amount": "10.00", "expected_balance": first["total"]},
        headers=waiter,
    )
    assert partial_payment.status_code == 201

    response = await client.post(
        f"{ORDERS_URL}/{second['id']}/merge", json={"source_order_id": first["id"]}, headers=waiter
    )
    with_itself = await client.post(
        f"{ORDERS_URL}/{second['id']}/merge", json={"source_order_id": second["id"]}, headers=waiter
    )

    assert response.status_code == 409
    assert with_itself.status_code == 422


async def test_served_dish_is_not_deducted_twice_on_merge(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter, owner = authorization_for(restaurant_a.waiter), authorization_for(restaurant_a.admin)
    ingredient = await client.post(
        url("inventory/ingredients"),
        json={"name": "Lomo fino", "unit": "g", "min_stock": "0", "unit_cost": "0.05"},
        headers=owner,
    )
    ingredient_id = ingredient.json()["id"]
    await client.put(
        url(f"inventory/recipes/{menu_a.lomo}"),
        json={"lines": [{"ingredient_id": ingredient_id, "quantity": "200"}]},
        headers=owner,
    )
    await client.post(
        url("inventory/purchases"),
        json={"ingredient_id": ingredient_id, "quantity": "1000", "unit_cost": "0.05"},
        headers=owner,
    )
    first = await _order_at_table(client, waiter, menu_a.table_1, (menu_a.lomo, 1))
    second = await _order_at_table(client, waiter, menu_a.table_2, (menu_a.lomo, 1))
    for order in (first, second):
        for step, actor in (("send", waiter), ("ready", owner), ("served", waiter)):
            await _step(client, actor, order["id"], step)

    await client.post(
        f"{ORDERS_URL}/{first['id']}/merge", json={"source_order_id": second["id"]}, headers=waiter
    )
    await client.post(
        f"{ORDERS_URL}/{first['id']}/items",
        json={"items": [{"menu_item_id": menu_a.aji}]},
        headers=waiter,
    )
    for step, actor in (("ready", owner), ("served", waiter)):
        await _step(client, actor, first["id"], step)
    movements = await client.get(
        url("inventory/movements"),
        params={"ingredient_id": ingredient_id, "kind": "consumption"},
        headers=owner,
    )

    # Dos lomos servidos, dos consumos: unir y volver a servir no descuenta de nuevo.
    assert movements.json()["total"] == 2


async def open_cash_session_http(client: AsyncClient, restaurant: StaffedRestaurant) -> None:
    response = await client.post(
        url("cash/open"), json={"opening_amount": "0"}, headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 201, response.text


async def test_waiter_cannot_merge_a_colleagues_table_into_own(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    carla = await SqlAlchemyUserRepository(session).add(
        build_user(
            restaurant_a.id, "carla@local-a.pe", restaurant_a.waiter.role, full_name="Carla Ríos"
        )
    )
    await session.commit()
    waiter, colleague = authorization_for(restaurant_a.waiter), authorization_for(carla)
    own = await _order_at_table(client, waiter, menu_a.table_1, (menu_a.lomo, 1))
    foreign = await _order_at_table(client, colleague, menu_a.table_2, (menu_a.aji, 1))

    take_foreign = await client.post(
        f"{ORDERS_URL}/{own['id']}/merge", json={"source_order_id": foreign["id"]}, headers=waiter
    )
    give_own = await client.post(
        f"{ORDERS_URL}/{foreign['id']}/merge", json={"source_order_id": own["id"]}, headers=waiter
    )
    by_owner = await client.post(
        f"{ORDERS_URL}/{own['id']}/merge",
        json={"source_order_id": foreign["id"]},
        headers=authorization_for(restaurant_a.admin),
    )

    assert take_foreign.status_code == 403
    assert "unir esas mesas" in take_foreign.json()["detail"]
    assert give_own.status_code == 403
    assert by_owner.status_code == 200, by_owner.text
