"""Anular un cobro mal hecho (RFS-25): permiso de caja, motivo y el arqueo al día."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from tests.builders import SeededMenu, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url
from tests.fakes_lycet import FakeLycet, configure_billing, paid_order, use_fake_lycet
from tests.test_api_cash import CASH_URL, ORDERS_URL, _open_cash_register, _pay, _served

REASON = "Se marcó Yape y pagó en efectivo"


@pytest.fixture
async def menu(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_a.id)


def _void_url(order_id: int, payment_id: int) -> str:
    return f"{ORDERS_URL}/{order_id}/payments/{payment_id}/void"


async def test_voiding_reopens_the_bill_and_leaves_the_register_without_it(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a, "100.00")
    owner = authorization_for(restaurant_a.admin)
    served = await _served(client, restaurant_a, menu)
    paid = await _pay(client, owner, served["id"], payment_method="yape", tip="2.00")
    target = _void_url(served["id"], paid["payments"][0]["id"])

    voided = await client.post(target, json={"reason": REASON}, headers=owner)
    twice = await client.post(target, json={"reason": REASON}, headers=owner)
    again = await _pay(client, owner, served["id"], payment_method="cash")
    current = await client.get(f"{CASH_URL}/current", headers=owner)
    log = await client.get(
        url("activity"), params={"kind": ActivityKind.PAYMENT_VOIDED.value}, headers=owner
    )

    body = voided.json()
    assert voided.status_code == 200, voided.text
    assert (body["status"], body["balance"], body["payments"]) == ("served", "61.50", [])
    assert body["voided_payments"][0]["void_reason"] == REASON
    assert body["voided_payments"][0]["voided_by_name"]
    assert twice.json()["code"] == "payment_not_found"
    assert again["status"] == "paid"
    # Solo el cobro bueno: el anulado no entró a la caja, ni su propina.
    by_method = current.json()["session"]["summary"]["by_method"]
    assert [(m["method"], m["amount"], m["tips"]) for m in by_method] == [("cash", "61.50", "0.00")]
    assert REASON in log.json()["items"][0]["detail"]


async def test_only_who_manages_the_register_and_with_a_reason(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    owner = authorization_for(restaurant_a.admin)
    served = await _served(client, restaurant_a, menu)
    paid = await _pay(client, owner, served["id"], payment_method="yape")
    target = _void_url(served["id"], paid["payments"][0]["id"])

    waiter = await client.post(
        target, json={"reason": REASON}, headers=authorization_for(restaurant_a.waiter)
    )
    empty = await client.post(target, json={"reason": ""}, headers=owner)

    assert waiter.status_code == 403
    assert empty.status_code == 422


async def test_a_payment_of_a_closed_shift_stays(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu: SeededMenu
) -> None:
    await _open_cash_register(client, restaurant_a)
    owner = authorization_for(restaurant_a.admin)
    served = await _served(client, restaurant_a, menu)
    paid = await _pay(client, owner, served["id"], payment_method="yape")
    closed = await client.post(f"{CASH_URL}/close", json={"counted_cash": "100.00"}, headers=owner)
    await _open_cash_register(client, restaurant_a)

    response = await client.post(
        _void_url(served["id"], paid["payments"][0]["id"]), json={"reason": REASON}, headers=owner
    )

    assert closed.status_code == 200
    assert (response.status_code, response.json()["code"]) == (409, "payment_not_voidable")


async def test_with_a_receipt_issued_the_receipt_goes_first(
    app: FastAPI, client: AsyncClient, restaurant_a: StaffedRestaurant, menu: SeededMenu
) -> None:
    use_fake_lycet(app, FakeLycet())
    await configure_billing(client, restaurant_a)
    await _open_cash_register(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu)
    owner = authorization_for(restaurant_a.admin)
    issued = await client.post(
        url("billing/invoices"), json={"order_id": order["id"], "kind": "boleta"}, headers=owner
    )

    response = await client.post(
        _void_url(order["id"], order["payments"][0]["id"]), json={"reason": REASON}, headers=owner
    )

    assert issued.status_code == 201, issued.text
    assert (response.status_code, response.json()["code"]) == (409, "order_already_invoiced")
