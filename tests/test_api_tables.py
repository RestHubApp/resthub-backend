"""Mesas por HTTP: estado libre/ocupada, permisos y aislamiento."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url

TABLES_URL = url("tables")


async def test_create_rename_reorder_and_deactivate(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    one = (await client.post(TABLES_URL, json={"label": "1"}, headers=admin)).json()
    bar = (await client.post(TABLES_URL, json={"label": "Barra"}, headers=admin)).json()

    duplicate = await client.post(TABLES_URL, json={"label": "barra"}, headers=admin)
    renamed = await client.patch(
        f"{TABLES_URL}/{one['id']}", json={"label": "Terraza 1"}, headers=admin
    )
    reordered = await client.put(
        f"{TABLES_URL}/order", json={"ids": [bar["id"], one["id"]]}, headers=admin
    )
    await client.patch(f"{TABLES_URL}/{bar['id']}", json={"is_active": False}, headers=admin)

    assert duplicate.status_code == 409
    assert renamed.json()["label"] == "Terraza 1"
    assert [t["label"] for t in reordered.json()] == ["Barra", "Terraza 1"]
    visible = await client.get(TABLES_URL, headers=authorization_for(restaurant_a.waiter))
    all_tables = await client.get(TABLES_URL, params={"include_inactive": True}, headers=admin)
    assert [t["label"] for t in visible.json()] == ["Terraza 1"]
    assert len(all_tables.json()) == 2


async def test_table_shows_its_active_order(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    menu = await seed_menu(session, restaurant_a.id)
    waiter = authorization_for(restaurant_a.waiter)
    order = await client.post(
        url("orders"),
        json={"type": "dine_in", "table_id": menu.table_2, "items": [{"menu_item_id": menu.lomo}]},
        headers=waiter,
    )

    response = await client.get(TABLES_URL, headers=waiter)

    free, occupied = response.json()
    assert (free["status"], free["status_label"], free["active_order"]) == (
        "free",
        "Libre",
        None,
    )
    assert occupied["status"] == "occupied"
    assert occupied["active_order"]["id"] == order.json()["id"]
    assert occupied["active_order"]["total"] == "28.00"
    assert occupied["active_order"]["waiter_name"] == "Luis Torres"
    assert occupied["active_order"]["status_changed_at"] == order.json()["status_changed_at"]


async def test_deactivated_table_receives_no_orders(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    menu = await seed_menu(session, restaurant_a.id)
    await client.patch(
        f"{TABLES_URL}/{menu.table_1}",
        json={"is_active": False},
        headers=authorization_for(restaurant_a.admin),
    )

    response = await client.post(
        url("orders"),
        json={"type": "dine_in", "table_id": menu.table_1},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("POST", "", {"label": "9"}),
        ("PATCH", "/1", {"label": "9"}),
        ("PUT", "/order", {"ids": []}),
    ],
)
async def test_waiter_does_not_manage_tables(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    method: str,
    suffix: str,
    body: dict[str, object],
) -> None:
    response = await client.request(
        method, TABLES_URL + suffix, json=body, headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 403


async def test_restaurant_neither_sees_nor_touches_tables_of_another(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    menu_b = await seed_menu(session, restaurant_b.id)
    admin_a = authorization_for(restaurant_a.admin)

    edit = await client.patch(
        f"{TABLES_URL}/{menu_b.table_1}", json={"label": "X"}, headers=admin_a
    )
    listing = await client.get(TABLES_URL, headers=admin_a)
    reorder = await client.put(
        f"{TABLES_URL}/order", json={"ids": [menu_b.table_1]}, headers=admin_a
    )

    assert edit.status_code == 404
    assert listing.json() == []
    assert reorder.status_code == 422
