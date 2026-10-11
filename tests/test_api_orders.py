"""Pedidos por HTTP: reglas, permisos del mesero y del encargado, aislamiento."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.realtime_broker import LocalBroker
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.orders.domain.cash import CashSession
from tests.builders import SeededMenu, open_cash_session, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, build_user, url

ORDERS_URL = url("orders")


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_a.id)


@pytest.fixture
async def cash_session_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> CashSession:
    return await open_cash_session(session, restaurant_a.id, restaurant_a.admin.id or 0)


@pytest.fixture
async def menu_b(session: AsyncSession, restaurant_b: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_b.id)


async def _open(
    client: AsyncClient,
    headers: dict[str, str],
    table_id: int | None,
    *items: tuple[int, int],
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "dine_in" if table_id else "takeaway",
        "table_id": table_id,
        "items": [{"menu_item_id": dish, "quantity": quantity} for dish, quantity in items],
        **extra,
    }
    response = await client.post(ORDERS_URL, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _step(
    client: AsyncClient, headers: dict[str, str], order_id: int, action: str, **body: Any
) -> dict[str, Any]:
    response = await client.post(
        f"{ORDERS_URL}/{order_id}/{action}", json=body or None, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _moment(value: str) -> datetime:
    # Se comparan como fechas y no como texto: sin microsegundos, el texto
    # ISO pierde la parte decimal y el orden alfabético deja de ser el real.
    return datetime.fromisoformat(value)


async def _served_order(
    client: AsyncClient, restaurant: StaffedRestaurant, menu: SeededMenu
) -> dict[str, Any]:
    waiter, owner = authorization_for(restaurant.waiter), authorization_for(restaurant.admin)
    order = await _open(client, waiter, menu.table_1, (menu.lomo, 2), (menu.chicha, 1))
    await _step(client, waiter, order["id"], "send")
    await _step(client, owner, order["id"], "ready")
    return await _step(client, waiter, order["id"], "served")


async def test_waiter_opens_an_order_with_menu_snapshot(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    order = await _open(
        client,
        authorization_for(restaurant_a.waiter),
        menu_a.table_1,
        (menu_a.lomo, 2),
        (menu_a.chicha, 1),
        notes="Cumpleaños",
    )

    assert order["status"] == "open"
    assert order["status_label"] == "Abierto"
    assert order["number"] == 1
    assert order["table_label"] == "1"
    assert order["waiter_id"] == restaurant_a.waiter.id
    assert order["waiter_name"] == "Luis Torres"
    assert order["total"] == "61.50"
    assert [(i["name"], i["unit_price"], i["subtotal"]) for i in order["items"]] == [
        ("Lomo saltado", "28.00", "56.00"),
        ("Chicha morada", "5.50", "5.50"),
    ]


async def test_number_is_sequential_per_restaurant(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_a: SeededMenu,
    menu_b: SeededMenu,
) -> None:
    waiter_a, waiter_b = (
        authorization_for(restaurant_a.waiter),
        authorization_for(restaurant_b.waiter),
    )

    first = await _open(client, waiter_a, menu_a.table_1, (menu_a.lomo, 1))
    second = await _open(client, waiter_a, None, (menu_a.aji, 1), customer_name="Rosa")
    other_restaurant = await _open(client, waiter_b, menu_b.table_1, (menu_b.lomo, 1))

    assert (first["number"], second["number"], other_restaurant["number"]) == (1, 2, 1)
    assert second["type"] == "takeaway"
    assert second["table_id"] is None
    assert second["customer_name"] == "Rosa"


async def test_a_table_has_only_one_active_order(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    first = await _open(client, waiter, menu_a.table_1, (menu_a.lomo, 1))

    occupied = await client.post(
        ORDERS_URL, json={"type": "dine_in", "table_id": menu_a.table_1}, headers=waiter
    )
    assert occupied.status_code == 409

    await client.post(
        f"{ORDERS_URL}/{first['id']}/cancel",
        json={"reason": "Se equivocó de mesa"},
        headers=authorization_for(restaurant_a.admin),
    )
    released = await client.post(
        ORDERS_URL, json={"type": "dine_in", "table_id": menu_a.table_1}, headers=waiter
    )
    assert released.status_code == 201


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"type": "dine_in"}, 422),
        ({"type": "takeaway", "table_id": "{mesa}"}, 422),
    ],
)
async def test_table_and_type_must_match(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    body: dict[str, Any],
    expected: int,
) -> None:
    if body.get("table_id") == "{mesa}":
        body = {**body, "table_id": menu_a.table_1}

    response = await client.post(
        ORDERS_URL, json=body, headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == expected


async def test_only_active_and_available_dishes(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, menu_b: SeededMenu
) -> None:
    waiter = authorization_for(restaurant_a.waiter)

    async def place_order(dish: int) -> int:
        response = await client.post(
            ORDERS_URL,
            json={"type": "takeaway", "items": [{"menu_item_id": dish}]},
            headers=waiter,
        )
        return response.status_code

    assert await place_order(menu_a.sold_out) == 409
    assert await place_order(menu_a.withdrawn) == 409
    # Un plato de otro local es, para este, un plato que no existe.
    assert await place_order(menu_b.lomo) == 404


async def test_full_flow_with_cash_charge(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    cash_session_a: CashSession,
) -> None:
    served = await _served_order(client, restaurant_a, menu_a)
    assert served["status"] == "served"

    paid = await _step(
        client,
        authorization_for(restaurant_a.admin),
        served["id"],
        "charge",
        payment_method="cash",
        amount_received="100",
    )

    assert paid["status"] == "paid"
    assert paid["total"] == "61.50"
    assert paid["amount_received"] == "100.00"
    assert paid["change"] == "38.50"
    assert paid["payment_method_label"] == "Efectivo"
    assert paid["paid_at"] is not None
    tables = await client.get(url("tables"), headers=authorization_for(restaurant_a.waiter))
    assert tables.json()[0]["status"] == "free"


async def test_insufficient_cash_and_amount_on_yape_are_rejected(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    cash_session_a: CashSession,
) -> None:
    served = await _served_order(client, restaurant_a, menu_a)
    owner = authorization_for(restaurant_a.admin)
    url = f"{ORDERS_URL}/{served['id']}/charge"

    short_cash = await client.post(
        url, json={"payment_method": "cash", "amount_received": "50.00"}, headers=owner
    )
    yape = await client.post(
        url, json={"payment_method": "yape", "amount_received": "61.50"}, headers=owner
    )
    valid = await client.post(url, json={"payment_method": "yape"}, headers=owner)

    assert short_cash.status_code == 422
    assert yape.status_code == 422
    assert valid.status_code == 200
    assert valid.json()["change"] is None


async def test_adding_dishes_to_served_order_sends_it_back_to_kitchen(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    served = await _served_order(client, restaurant_a, menu_a)

    response = await client.post(
        f"{ORDERS_URL}/{served['id']}/items",
        json={"items": [{"menu_item_id": menu_a.aji, "quantity": 1, "notes": "sin ají"}]},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "in_kitchen"
    assert response.json()["total"] == "83.50"
    assert response.json()["items"][-1]["notes"] == "sin ají"


async def test_edit_and_remove_only_while_order_is_open(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    order = await _open(client, waiter, menu_a.table_1, (menu_a.lomo, 1), (menu_a.chicha, 1))
    lomo, chicha = (item["id"] for item in order["items"])

    changed = await client.patch(
        f"{ORDERS_URL}/{order['id']}/items/{lomo}",
        json={"quantity": 3, "notes": "término medio"},
        headers=waiter,
    )
    removed = await client.delete(f"{ORDERS_URL}/{order['id']}/items/{chicha}", headers=waiter)
    assert changed.status_code == 200
    assert removed.json()["total"] == "84.00"

    await _step(client, waiter, order["id"], "send")
    late = await client.patch(
        f"{ORDERS_URL}/{order['id']}/items/{lomo}", json={"quantity": 1}, headers=waiter
    )
    assert late.status_code == 409


async def test_editing_note_does_not_reset_time_in_status(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    order = await _open(client, waiter, menu_a.table_1, (menu_a.lomo, 1))
    assert order["status_changed_at"] == order["created_at"]
    sent = await _step(client, waiter, order["id"], "send")

    edited = await client.patch(
        f"{ORDERS_URL}/{order['id']}", json={"notes": "sin cebolla"}, headers=waiter
    )

    assert edited.status_code == 200
    assert _moment(sent["status_changed_at"]) > _moment(order["status_changed_at"])
    assert edited.json()["status_changed_at"] == sent["status_changed_at"]
    assert _moment(edited.json()["updated_at"]) > _moment(sent["updated_at"])


async def test_invalid_transition_responds_409(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    order = await _open(client, waiter, menu_a.table_1, (menu_a.lomo, 1))
    empty = await _open(client, waiter, menu_a.table_2)

    serve = await client.post(f"{ORDERS_URL}/{order['id']}/served", headers=waiter)
    send_empty = await client.post(f"{ORDERS_URL}/{empty['id']}/send", headers=waiter)

    assert serve.status_code == 409
    assert send_empty.status_code == 422


async def test_cancel_requires_reason_and_is_logged(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    owner = authorization_for(restaurant_a.admin)
    order = await _open(
        client, authorization_for(restaurant_a.waiter), menu_a.table_1, (menu_a.lomo, 1)
    )

    no_reason = await client.post(
        f"{ORDERS_URL}/{order['id']}/cancel", json={"reason": "   "}, headers=owner
    )
    cancelled = await _step(client, owner, order["id"], "cancel", reason="Cliente se fue")

    assert no_reason.status_code == 422
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_reason"] == "Cliente se fue"
    activity_log = await client.get(url("activity"), headers=owner)
    assert activity_log.json()["items"][0]["kind"] == ActivityKind.ORDER_CANCELLED.value


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("ready", None),
        ("cancel", {"reason": "No quiero"}),
    ],
)
async def test_waiter_does_not_manage_the_kitchen(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    action: str,
    body: dict[str, Any] | None,
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    order = await _open(client, waiter, menu_a.table_1, (menu_a.lomo, 1))

    response = await client.post(f"{ORDERS_URL}/{order['id']}/{action}", json=body, headers=waiter)

    assert response.status_code == 403


async def test_waiter_sees_own_and_active_owner_sees_all(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    cash_session_a: CashSession,
) -> None:
    other_user = await SqlAlchemyUserRepository(session).add(
        build_user(
            restaurant_a.id, "carla@local-a.pe", restaurant_a.waiter.role, full_name="Carla Ríos"
        )
    )
    await session.commit()
    waiter, colleague = authorization_for(restaurant_a.waiter), authorization_for(other_user)
    owner = authorization_for(restaurant_a.admin)

    charged = await _served_order(client, restaurant_a, menu_a)
    await _step(client, owner, charged["id"], "charge", payment_method="card")
    active = await _open(client, waiter, menu_a.table_2, (menu_a.aji, 1))
    own = await _open(client, colleague, None, (menu_a.chicha, 2))

    listing = await client.get(ORDERS_URL, headers=colleague)
    foreign_charged = await client.get(f"{ORDERS_URL}/{charged['id']}", headers=colleague)
    foreign_active = await client.get(f"{ORDERS_URL}/{active['id']}", headers=colleague)
    all_orders = await client.get(ORDERS_URL, headers=owner)
    paid_orders = await client.get(ORDERS_URL, params={"status": "paid"}, headers=owner)

    assert {o["id"] for o in listing.json()["items"]} == {active["id"], own["id"]}
    assert foreign_charged.status_code == 404
    assert foreign_active.status_code == 200
    assert all_orders.json()["total"] == 3
    assert [o["id"] for o in paid_orders.json()["items"]] == [charged["id"]]


async def test_filter_by_restaurant_date(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    order = await _open(client, authorization_for(restaurant_a.waiter), None, (menu_a.lomo, 1))
    owner = authorization_for(restaurant_a.admin)

    today = await client.get(
        ORDERS_URL,
        params={"date_from": order["business_date"], "date_to": order["business_date"]},
        headers=owner,
    )
    before = await client.get(ORDERS_URL, params={"date_to": "2000-01-01"}, headers=owner)

    assert today.json()["total"] == 1
    assert before.json()["total"] == 0


async def test_board_returns_active_in_arrival_order(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    first = await _open(client, waiter, menu_a.table_1, (menu_a.lomo, 1))
    second = await _open(client, waiter, menu_a.table_2, (menu_a.aji, 1))
    cancelled = await _open(client, waiter, None, (menu_a.chicha, 1))
    await _step(
        client, authorization_for(restaurant_a.admin), cancelled["id"], "cancel", reason="x"
    )

    response = await client.get(f"{ORDERS_URL}/active", headers=waiter)

    assert [o["id"] for o in response.json()] == [first["id"], second["id"]]
    assert response.json()[0]["items"][0]["name"] == "Lomo saltado"


async def test_every_change_notifies_the_board(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, broker: LocalBroker
) -> None:
    async with broker.subscribe() as queue:
        order = await _open(client, authorization_for(restaurant_a.waiter), None, (menu_a.lomo, 1))
        await _step(client, authorization_for(restaurant_a.waiter), order["id"], "send")
        await asyncio.sleep(0)

        notices = [queue.get_nowait(), queue.get_nowait()]

    assert {notice.topic for notice in notices} == {"orders"}
    assert {notice.reference_id for notice in notices} == {order["id"]}
    assert all(notice.restaurant_id == restaurant_a.id for notice in notices)
    assert all(notice.everyone for notice in notices)


@pytest.mark.parametrize(
    ("method", "suffix", "body", "who"),
    [
        ("GET", "", None, "admin"),
        ("PATCH", "", {"notes": "intruso"}, "admin"),
        ("POST", "/items", {"items": [{"menu_item_id": 1}]}, "waiter"),
        ("POST", "/send", None, "waiter"),
        ("POST", "/ready", None, "admin"),
        ("POST", "/served", None, "waiter"),
        ("POST", "/charge", {"payment_method": "cash"}, "admin"),
        ("POST", "/payments", {"payment_method": "yape", "amount": "5.00"}, "admin"),
        ("PUT", "/discount", {"percent": "5", "reason": "intruso"}, "admin"),
        ("PUT", "/items/1/courtesy", {"reason": "intruso"}, "admin"),
        ("POST", "/cancel", {"reason": "intruso"}, "admin"),
    ],
)
async def test_a_restaurant_cannot_see_or_touch_anothers_orders(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_b: SeededMenu,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
    who: str,
) -> None:
    foreign = await _open(
        client, authorization_for(restaurant_b.waiter), menu_b.table_1, (menu_b.lomo, 1)
    )
    intruder = restaurant_a.admin if who == "admin" else restaurant_a.waiter

    response = await client.request(
        method,
        f"{ORDERS_URL}/{foreign['id']}{suffix}",
        json=body,
        headers=authorization_for(intruder),
    )

    assert response.status_code == 404
    untouched = await client.get(
        f"{ORDERS_URL}/{foreign['id']}", headers=authorization_for(restaurant_b.admin)
    )
    assert untouched.json()["status"] == "open"
    assert untouched.json()["notes"] == ""
    listing = await client.get(ORDERS_URL, headers=authorization_for(restaurant_a.admin))
    assert listing.json()["total"] == 0


async def test_order_cannot_be_opened_on_another_restaurants_table(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, menu_b: SeededMenu
) -> None:
    response = await client.post(
        ORDERS_URL,
        json={"type": "dine_in", "table_id": menu_b.table_1},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 404
