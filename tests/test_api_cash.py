"""Caja y cobro por HTTP: turnos, cuenta dividida, pago mixto, descuentos y propinas."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.realtime_broker import LocalBroker
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from tests.builders import SeededMenu, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, build_user, url

ORDERS_URL = url("orders")
CASH_URL = url("cash")


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_a.id)


def _admin(local: StaffedRestaurant) -> dict[str, str]:
    return authorization_for(local.admin)


def _waiter(local: StaffedRestaurant) -> dict[str, str]:
    return authorization_for(local.waiter)


async def _open_cash_register(
    client: AsyncClient, local: StaffedRestaurant, opening: str = "100.00"
) -> dict[str, Any]:
    response = await client.post(
        f"{CASH_URL}/open", json={"opening_amount": opening}, headers=_admin(local)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _served(
    client: AsyncClient,
    local: StaffedRestaurant,
    menu: SeededMenu,
    headers: dict[str, str] | None = None,
    items: tuple[tuple[int, int], ...] | None = None,
) -> dict[str, Any]:
    """Un pedido para llevar ya servido. Por omisión: 2 lomos y 1 chicha (61.50)."""
    waiter = headers or _waiter(local)
    lines = items or ((menu.lomo, 2), (menu.chicha, 1))
    created = await client.post(
        ORDERS_URL,
        json={
            "type": "takeaway",
            "items": [{"menu_item_id": dish, "quantity": q} for dish, q in lines],
        },
        headers=waiter,
    )
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]
    for step, who in (("send", waiter), ("ready", _admin(local)), ("served", waiter)):
        response = await client.post(f"{ORDERS_URL}/{order_id}/{step}", headers=who)
        assert response.status_code == 200, response.text
    return response.json()


async def _pay(
    client: AsyncClient, headers: dict[str, str], order_id: int, **body: Any
) -> dict[str, Any]:
    if "amount" in body and "expected_balance" not in body:
        # Como la pantalla de cobro: una parte libre va con el saldo que se vio.
        order = await client.get(f"{ORDERS_URL}/{order_id}", headers=headers)
        body["expected_balance"] = order.json()["balance"]
    response = await client.post(f"{ORDERS_URL}/{order_id}/payments", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# -- Turnos -------------------------------------------------------------------


async def test_no_open_cash_register_means_no_charge(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    served = await _served(client, restaurant_a, menu_a)

    response = await client.post(
        f"{ORDERS_URL}/{served['id']}/charge",
        json={"payment_method": "yape"},
        headers=_admin(restaurant_a),
    )
    current = await client.get(f"{CASH_URL}/current", headers=_waiter(restaurant_a))

    assert response.status_code == 409
    assert "caja está cerrada" in response.json()["detail"]
    assert current.json() == {"is_open": False, "session": None}


async def test_only_owner_opens_and_only_one_register_open(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    waiter = await client.post(
        f"{CASH_URL}/open", json={"opening_amount": "50"}, headers=_waiter(restaurant_a)
    )
    open_session = await _open_cash_register(client, restaurant_a, "150.00")
    other = await client.post(
        f"{CASH_URL}/open", json={"opening_amount": "10"}, headers=_admin(restaurant_a)
    )
    # Cada local tiene su propia caja.
    other_restaurant_session = await _open_cash_register(client, restaurant_b)

    assert waiter.status_code == 403
    assert open_session["is_open"] is True
    assert open_session["opening_amount"] == "150.00"
    assert open_session["opened_by_name"] == "Rosa Pérez"
    assert open_session["summary"]["expected_cash"] == "150.00"
    assert other.status_code == 409
    assert other_restaurant_session["id"] != open_session["id"]


async def test_waiter_knows_register_is_open_but_sees_no_amounts(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _open_cash_register(client, restaurant_a)

    by_waiter = await client.get(f"{CASH_URL}/current", headers=_waiter(restaurant_a))
    by_owner = await client.get(f"{CASH_URL}/current", headers=_admin(restaurant_a))
    history = await client.get(f"{CASH_URL}/sessions", headers=_waiter(restaurant_a))

    assert by_waiter.json() == {"is_open": True, "session": None}
    assert by_owner.json()["session"]["opening_amount"] == "100.00"
    assert history.status_code == 403


async def test_opening_register_notifies_all_staff(
    client: AsyncClient, restaurant_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    async with broker.subscribe() as queue:
        open_session = await _open_cash_register(client, restaurant_a)
        await asyncio.sleep(0)
        notice = queue.get_nowait()

    assert notice.topic == "cash"
    assert notice.reference_id == open_session["id"]


# -- Quién cobra --------------------------------------------------------------


async def test_waiter_charges_own_orders_not_colleagues(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    colleague = await SqlAlchemyUserRepository(session).add(
        build_user(
            restaurant_a.id, "carla@local-a.pe", restaurant_a.waiter.role, full_name="Carla Ríos"
        )
    )
    await session.commit()
    await _open_cash_register(client, restaurant_a)
    own = await _served(client, restaurant_a, menu_a)
    foreign = await _served(client, restaurant_a, menu_a, headers=authorization_for(colleague))

    charged = await client.post(
        f"{ORDERS_URL}/{own['id']}/charge",
        json={"payment_method": "plin"},
        headers=_waiter(restaurant_a),
    )
    rejected = await client.post(
        f"{ORDERS_URL}/{foreign['id']}/charge",
        json={"payment_method": "plin"},
        headers=_waiter(restaurant_a),
    )
    by_owner = await client.post(
        f"{ORDERS_URL}/{foreign['id']}/charge",
        json={"payment_method": "card"},
        headers=_admin(restaurant_a),
    )

    assert charged.status_code == 200
    assert charged.json()["payments"][0]["received_by_name"] == "Luis Torres"
    assert rejected.status_code == 403
    assert by_owner.status_code == 200
    assert by_owner.json()["status"] == "paid"


async def test_double_tap_does_not_charge_twice(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    served = await _served(client, restaurant_a, menu_a)
    url = f"{ORDERS_URL}/{served['id']}/payments"
    part = {"payment_method": "yape", "amount": "20.50", "expected_balance": "61.50"}

    first = await client.post(url, json=part, headers=_waiter(restaurant_a))
    second = await client.post(url, json=part, headers=_waiter(restaurant_a))

    assert first.status_code == 201
    assert first.json()["balance"] == "41.00"
    assert second.status_code == 409
    assert "S/ 41.00" in second.json()["detail"]


# -- Cuenta dividida y pago mixto ----------------------------------------------


async def test_bill_split_in_equal_parts_with_mixed_payment(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    served = await _served(client, restaurant_a, menu_a)
    waiter = _waiter(restaurant_a)

    # 61.50 entre tres: 20.50 cada uno.
    one = await _pay(client, waiter, served["id"], payment_method="cash", amount="20.50")
    two = await _pay(client, waiter, served["id"], payment_method="yape", amount="20.50")
    cancel_response = await client.post(
        f"{ORDERS_URL}/{served['id']}/cancel",
        json={"reason": "Se fueron"},
        headers=_admin(restaurant_a),
    )
    three = await _pay(client, waiter, served["id"], payment_method="yape")

    assert one["status"] == "served"
    assert one["balance"] == "41.00"
    assert two["paid_amount"] == "41.00"
    assert cancel_response.status_code == 409
    assert three["status"] == "paid"
    assert three["balance"] == "0.00"
    assert three["payments"][2]["amount"] == "20.50"
    assert three["payment_method"] is None
    assert three["payment_method_label"] == "Mixto"


async def test_bill_split_by_dishes(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    served = await _served(client, restaurant_a, menu_a, items=((menu_a.lomo, 1), (menu_a.aji, 1)))
    lomo, aji = (item["id"] for item in served["items"])
    waiter = _waiter(restaurant_a)

    first = await _pay(client, waiter, served["id"], payment_method="card", item_ids=[lomo])
    repeated = await client.post(
        f"{ORDERS_URL}/{served['id']}/payments",
        json={"payment_method": "cash", "item_ids": [lomo]},
        headers=waiter,
    )
    second = await _pay(
        client, waiter, served["id"], payment_method="cash", item_ids=[aji], amount_received="30"
    )

    assert first["payments"][0]["amount"] == "28.00"
    assert [item["is_paid"] for item in first["items"]] == [True, False]
    assert repeated.status_code == 422
    assert second["status"] == "paid"
    assert second["payments"][1]["amount"] == "22.00"
    assert second["payments"][1]["change"] == "8.00"


async def test_part_cannot_exceed_remaining(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    served = await _served(client, restaurant_a, menu_a)

    response = await client.post(
        f"{ORDERS_URL}/{served['id']}/payments",
        json={"payment_method": "yape", "amount": "70.00", "expected_balance": served["balance"]},
        headers=_waiter(restaurant_a),
    )

    assert response.status_code == 422


# -- Propinas -------------------------------------------------------------------


async def test_tip_is_separate_from_sale(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    served = await _served(client, restaurant_a, menu_a)

    paid = await client.post(
        f"{ORDERS_URL}/{served['id']}/charge",
        json={"payment_method": "cash", "amount_received": "70.00", "tip": "5.00"},
        headers=_waiter(restaurant_a),
    )
    short = await _served(client, restaurant_a, menu_a)
    uncovered = await client.post(
        f"{ORDERS_URL}/{short['id']}/charge",
        json={"payment_method": "cash", "amount_received": "62.00", "tip": "5.00"},
        headers=_waiter(restaurant_a),
    )

    body = paid.json()
    assert body["total"] == "61.50"
    assert body["tips"] == "5.00"
    # Entregó 70: 61.50 de la cuenta, 5 de propina, 3.50 de vuelto.
    assert body["change"] == "3.50"
    assert uncovered.status_code == 422


# -- Descuentos y cortesías -----------------------------------------------------


async def test_waiter_discounts_up_to_limit_and_owner_without_limit(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    served = await _served(client, restaurant_a, menu_a)
    url = f"{ORDERS_URL}/{served['id']}/discount"

    no_reason = await client.put(url, json={"percent": "10"}, headers=_waiter(restaurant_a))
    over_limit = await client.put(
        url, json={"percent": "15", "reason": "Cliente frecuente"}, headers=_waiter(restaurant_a)
    )
    within_limit = await client.put(
        url, json={"percent": "10", "reason": "Cliente frecuente"}, headers=_waiter(restaurant_a)
    )
    by_owner = await client.put(
        url, json={"percent": "30", "reason": "Demora en cocina"}, headers=_admin(restaurant_a)
    )

    assert no_reason.status_code == 422
    assert over_limit.status_code == 403
    assert "10.00 %" in over_limit.json()["detail"]
    assert within_limit.json()["discount_amount"] == "6.15"
    assert within_limit.json()["total"] == "55.35"
    assert by_owner.json()["discount_percent"] == "30.00"
    assert by_owner.json()["discount_amount"] == "18.45"
    assert by_owner.json()["total"] == "43.05"
    assert by_owner.json()["discounted_by_name"] == "Rosa Pérez"


async def test_owner_changes_waiter_discount_limit(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    served = await _served(client, restaurant_a, menu_a)
    limit = await client.patch(
        url("restaurant"), json={"max_waiter_discount_percent": "20"}, headers=_admin(restaurant_a)
    )

    after_raise = await client.put(
        f"{ORDERS_URL}/{served['id']}/discount",
        json={"percent": "15", "reason": "Cumpleaños"},
        headers=_waiter(restaurant_a),
    )

    assert limit.json()["max_waiter_discount_percent"] == "20.00"
    assert after_raise.status_code == 200


async def test_courtesies_belong_to_owner_and_are_not_charged(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    served = await _served(client, restaurant_a, menu_a)
    chicha = served["items"][1]["id"]
    url = f"{ORDERS_URL}/{served['id']}/items/{chicha}/courtesy"

    by_waiter = await client.put(
        url, json={"reason": "Invita la casa"}, headers=_waiter(restaurant_a)
    )
    comped = await client.put(url, json={"reason": "Invita la casa"}, headers=_admin(restaurant_a))
    undone = await client.delete(url, headers=_admin(restaurant_a))
    again = await client.put(url, json={"reason": "Invita la casa"}, headers=_admin(restaurant_a))
    await _pay(client, _waiter(restaurant_a), served["id"], payment_method="yape", amount="10.00")
    late = await client.delete(url, headers=_admin(restaurant_a))

    assert by_waiter.status_code == 403
    assert comped.json()["items"][1]["is_courtesy"] is True
    assert comped.json()["courtesy_amount"] == "5.50"
    assert comped.json()["total"] == "56.00"
    assert undone.json()["total"] == "61.50"
    assert again.json()["total"] == "56.00"
    # Con un pago hecho, cambiar lo que se cobra ya no cuadraría.
    assert late.status_code == 409


# -- Cierre y arqueo ------------------------------------------------------------


async def test_closing_reconciles_by_method_and_by_waiter(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a, "100.00")
    waiter = _waiter(restaurant_a)
    one = await _served(client, restaurant_a, menu_a)
    await client.put(
        f"{ORDERS_URL}/{one['id']}/discount",
        json={"percent": "10", "reason": "Cliente frecuente"},
        headers=waiter,
    )
    # 55.35: 30 en efectivo con 2 de propina, el resto por Yape con 3 de propina.
    await _pay(client, waiter, one["id"], payment_method="cash", amount="30.00", tip="2.00")
    await _pay(client, waiter, one["id"], payment_method="yape", tip="3.00")
    two = await _served(client, restaurant_a, menu_a, items=((menu_a.aji, 1),))
    await _pay(client, _admin(restaurant_a), two["id"], payment_method="cash")

    closing = await client.post(
        f"{CASH_URL}/close",
        json={"counted_cash": "150.00", "notes": "Faltó sencillo"},
        headers=_admin(restaurant_a),
    )
    later_order = await _served(client, restaurant_a, menu_a)
    no_register = await client.post(
        f"{ORDERS_URL}/{later_order['id']}/charge", json={"payment_method": "yape"}, headers=waiter
    )
    history = await client.get(f"{CASH_URL}/sessions", headers=_admin(restaurant_a))
    detail = await client.get(
        f"{CASH_URL}/sessions/{closing.json()['id']}", headers=_admin(restaurant_a)
    )
    activity_log = await client.get(
        url("activity"),
        params={"kind": ActivityKind.CASH_CLOSED.value},
        headers=_admin(restaurant_a),
    )

    body = closing.json()
    summary = body["summary"]
    assert closing.status_code == 200
    assert body["is_open"] is False
    assert [(m["method"], m["amount"], m["tips"]) for m in summary["by_method"]] == [
        ("cash", "52.00", "2.00"),
        ("yape", "25.35", "3.00"),
    ]
    assert summary["sales"] == "77.35"
    assert summary["tips"] == "5.00"
    assert summary["discounts"] == "6.15"
    assert summary["discounted_orders"] == 1
    assert summary["paid_orders"] == 2
    # 100 de inicial + 52 en efectivo + 2 de propina en efectivo.
    assert body["expected_cash"] == "154.00"
    assert body["counted_cash"] == "150.00"
    assert body["difference"] == "-4.00"
    # Toda la venta y las propinas son del mesero que atendió, aunque cobró el
    # encargado el segundo pedido.
    assert [(w["name"], w["sales"], w["tips"]) for w in summary["by_waiter"]] == [
        ("Luis Torres", "77.35", "5.00")
    ]
    assert no_register.status_code == 409
    assert history.json()["total"] == 1
    assert detail.json()["summary"]["sales"] == "77.35"
    assert activity_log.json()["items"][0]["kind"] == ActivityKind.CASH_CLOSED.value


async def test_cannot_close_register_that_is_not_open(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(
        f"{CASH_URL}/close", json={"counted_cash": "0"}, headers=_admin(restaurant_a)
    )

    assert response.status_code == 404


async def test_restaurant_cannot_see_shifts_of_another(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    foreign_session = await _open_cash_register(client, restaurant_b)

    detail = await client.get(
        f"{CASH_URL}/sessions/{foreign_session['id']}", headers=_admin(restaurant_a)
    )
    current = await client.get(f"{CASH_URL}/current", headers=_admin(restaurant_a))

    assert detail.status_code == 404
    assert current.json()["is_open"] is False


# -- Panel ----------------------------------------------------------------------


async def test_dashboard_splits_mixed_payment_and_separates_tips(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    served = await _served(client, restaurant_a, menu_a)
    waiter = _waiter(restaurant_a)
    await _pay(client, waiter, served["id"], payment_method="cash", amount="40.00", tip="4.00")
    await _pay(client, waiter, served["id"], payment_method="yape")

    methods = await client.get(url("insights/payments"), headers=_admin(restaurant_a))
    waiters = await client.get(url("insights/waiters"), headers=_admin(restaurant_a))
    summary = await client.get(url("insights/summary"), headers=_admin(restaurant_a))

    assert [(m["method"], m["amount"]) for m in methods.json()["methods"]] == [
        ("cash", "40.00"),
        ("yape", "21.50"),
    ]
    assert waiters.json()["waiters"][0]["tips"] == "4.00"
    assert summary.json()["sales"] == "61.50"
