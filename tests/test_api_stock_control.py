"""Control de mermas por HTTP: presentaciones, conteos físicos, diferencias y mermas por aprobar.

Contra la base de verdad: el stock que queda, quién puede cada cosa (403) y que
otro restaurante no ve ni toca nada (404).
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import Permission
from tests.conftest import StaffedRestaurant, authorization_for, staff_with_role, url

INVENTORY_URL = url("inventory")
BOTTLE = {"size": "750", "label": "botella", "full_weight_g": "1250", "empty_weight_g": "500"}


def _admin(restaurant: StaffedRestaurant) -> dict[str, str]:
    return authorization_for(restaurant.admin)


async def _ingredient(
    client: AsyncClient,
    restaurant: StaffedRestaurant,
    name: str,
    *,
    unit: str = "ml",
    unit_cost: str = "0.04",
    packaging: dict[str, str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "unit": unit, "unit_cost": unit_cost}
    if packaging is not None:
        body["packaging"] = packaging
    response = await client.post(
        f"{INVENTORY_URL}/ingredients", json=body, headers=_admin(restaurant)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _buy(
    client: AsyncClient, restaurant: StaffedRestaurant, ingredient: dict[str, Any], quantity: str
) -> None:
    response = await client.post(
        f"{INVENTORY_URL}/purchases",
        json={
            "ingredient_id": ingredient["id"],
            "quantity": quantity,
            "unit_cost": ingredient["unit_cost"],
        },
        headers=_admin(restaurant),
    )
    assert response.status_code == 201, response.text


async def _stock(client: AsyncClient, restaurant: StaffedRestaurant, ingredient_id: int) -> str:
    response = await client.get(
        f"{INVENTORY_URL}/ingredients/{ingredient_id}", headers=_admin(restaurant)
    )
    return str(response.json()["stock"])


# -- Presentaciones -----------------------------------------------------------------------


async def test_ingredient_packaging_is_saved_edited_and_removed(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco", packaging=BOTTLE)
    assert liquor["packaging"] == {
        "size": "750.000",
        "label": "botella",
        "full_weight_g": "1250.000",
        "empty_weight_g": "500.000",
        "weighable": True,
    }
    path = f"{INVENTORY_URL}/ingredients/{liquor['id']}"

    untouched = await client.patch(path, json={"min_stock": "750"}, headers=_admin(restaurant_a))
    edited = await client.patch(
        path, json={"packaging": {"size": "1000"}}, headers=_admin(restaurant_a)
    )
    invalid = await client.patch(
        path,
        json={"packaging": {"size": "750", "full_weight_g": "400", "empty_weight_g": "500"}},
        headers=_admin(restaurant_a),
    )
    removed = await client.patch(path, json={"packaging": None}, headers=_admin(restaurant_a))

    assert untouched.json()["packaging"]["size"] == "750.000"
    assert edited.json()["packaging"]["weighable"] is False
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_packaging"
    assert removed.json()["packaging"] is None


async def test_purchase_in_packages_is_converted_and_traced(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco", packaging=BOTTLE)
    rice = await _ingredient(client, restaurant_a, "Arroz", unit="g")

    response = await client.post(
        f"{INVENTORY_URL}/purchases",
        json={
            "ingredient_id": liquor["id"],
            "quantity": "6",
            "entry_mode": "packages",
            "unit_cost": "0.04",
        },
        headers=_admin(restaurant_a),
    )
    by_weight = await client.post(
        f"{INVENTORY_URL}/purchases",
        json={
            "ingredient_id": liquor["id"],
            "quantity": "900",
            "entry_mode": "weight",
            "unit_cost": "0.04",
        },
        headers=_admin(restaurant_a),
    )
    no_packaging = await client.post(
        f"{INVENTORY_URL}/purchases",
        json={
            "ingredient_id": rice["id"],
            "quantity": "2",
            "entry_mode": "packages",
            "unit_cost": "1",
        },
        headers=_admin(restaurant_a),
    )

    assert response.status_code == 201, response.text
    movement = response.json()["movement"]
    assert (movement["quantity"], movement["entry_mode"], movement["entry_quantity"]) == (
        "4500.000",
        "packages",
        "6.000",
    )
    assert response.json()["ingredient"]["stock"] == "4500.000"
    # El esquema solo ofrece `base` y `packages`: pesar es de los conteos.
    assert by_weight.status_code == 422
    assert by_weight.json()["code"] == "validation_error"
    assert no_packaging.json()["code"] == "invalid_quantity_entry"


# -- Conteos físicos ----------------------------------------------------------------------


async def test_full_stock_count_cycle_adjusts_and_reports_differences(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco", packaging=BOTTLE)
    lemon = await _ingredient(client, restaurant_a, "Limón", unit="unit", unit_cost="0.5")
    salt = await _ingredient(client, restaurant_a, "Sal", unit="g", unit_cost="0.002")
    await _buy(client, restaurant_a, liquor, "3000")
    await _buy(client, restaurant_a, lemon, "50")
    await _buy(client, restaurant_a, salt, "1000")
    headers = _admin(restaurant_a)

    opened = await client.post(
        f"{INVENTORY_URL}/stock-counts", json={"note": "Cierre de mes"}, headers=headers
    )
    assert opened.status_code == 201, opened.text
    count = opened.json()
    assert (count["number"], count["status"], count["total_lines"], count["counted_lines"]) == (
        1,
        "draft",
        3,
        0,
    )
    assert {line["ingredient_name"]: line["opening_stock"] for line in count["lines"]} == {
        "Pisco": "3000.000",
        "Limón": "50.000",
        "Sal": "1000.000",
    }
    count_url = f"{INVENTORY_URL}/stock-counts/{count['id']}"

    recorded = await client.put(
        f"{count_url}/lines",
        json={
            "lines": [
                {
                    "ingredient_id": liquor["id"],
                    "mode": "weight",
                    "quantity": "875",
                    "full_packages": "3",
                },
                {"ingredient_id": lemon["id"], "quantity": "55"},
                {"ingredient_id": salt["id"], "quantity": "999"},
            ]
        },
        headers=headers,
    )
    assert recorded.status_code == 200, recorded.text
    corrected = await client.put(
        f"{count_url}/lines",
        json={"lines": [{"ingredient_id": salt["id"], "quantity": None}]},
        headers=headers,
    )
    lines = {line["ingredient_name"]: line for line in corrected.json()["lines"]}
    assert lines["Pisco"]["counted"] == "2625.000"
    assert (lines["Pisco"]["entry_mode"], lines["Pisco"]["entry_full_packages"]) == (
        "weight",
        "3.000",
    )
    assert lines["Sal"]["counted"] is None
    assert corrected.json()["counted_lines"] == 2

    early_report = await client.get(f"{count_url}/differences", headers=headers)
    assert early_report.status_code == 409
    assert early_report.json()["code"] == "stock_count_not_closed"

    closed = await client.post(f"{count_url}/close", headers=headers)
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert await _stock(client, restaurant_a, liquor["id"]) == "2625.000"
    assert await _stock(client, restaurant_a, lemon["id"]) == "55.000"
    assert await _stock(client, restaurant_a, salt["id"]) == "1000.000"

    movements = await client.get(
        f"{INVENTORY_URL}/movements", params={"kind": "adjustment"}, headers=headers
    )
    assert {
        (m["ingredient_name"], m["quantity"], m["reason"], m["stock_count_id"])
        for m in movements.json()["items"]
    } == {
        ("Pisco", "-375.000", "Conteo físico #1", count["id"]),
        ("Limón", "5.000", "Conteo físico #1", count["id"]),
    }

    report = await client.get(f"{count_url}/differences", headers=headers)
    assert report.status_code == 200, report.text
    body = report.json()
    assert [line["ingredient_name"] for line in body["lines"]] == ["Pisco", "Limón"]
    assert (
        body["lines"][0]["expected"],
        body["lines"][0]["counted"],
        body["lines"][0]["difference"],
        body["lines"][0]["difference_percent"],
        body["lines"][0]["difference_value"],
    ) == ("3000.000", "2625.000", "-375.000", "-12.50", "-15.00")
    assert (body["total_value"], body["loss_value"], body["gain_value"]) == (
        "-12.50",
        "-15.00",
        "2.50",
    )

    history = await client.get(
        f"{INVENTORY_URL}/ingredients/{liquor['id']}/count-differences", headers=headers
    )
    assert history.status_code == 200, history.text
    assert [(i["count_number"], i["difference"]) for i in history.json()["items"]] == [
        (1, "-375.000")
    ]

    again = await client.put(
        f"{count_url}/lines",
        json={"lines": [{"ingredient_id": lemon["id"], "quantity": "1"}]},
        headers=headers,
    )
    assert again.status_code == 409
    assert again.json()["code"] == "stock_count_not_open"


async def test_only_one_count_is_open_and_a_cancelled_one_frees_the_way(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco")
    await _ingredient(client, restaurant_b, "Ron")
    first = await client.post(
        f"{INVENTORY_URL}/stock-counts",
        json={"scope": "selected", "ingredient_ids": [liquor["id"]]},
        headers=_admin(restaurant_a),
    )
    second = await client.post(
        f"{INVENTORY_URL}/stock-counts", json={}, headers=_admin(restaurant_a)
    )
    # Otro local tiene su propio conteo.
    other = await client.post(
        f"{INVENTORY_URL}/stock-counts", json={}, headers=_admin(restaurant_b)
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "stock_count_already_open"
    assert other.status_code == 201
    assert other.json()["number"] == 1

    cancelled = await client.post(
        f"{INVENTORY_URL}/stock-counts/{first.json()['id']}/cancel", headers=_admin(restaurant_a)
    )
    reopened = await client.post(
        f"{INVENTORY_URL}/stock-counts", json={}, headers=_admin(restaurant_a)
    )
    listed = await client.get(
        f"{INVENTORY_URL}/stock-counts",
        params={"status": "cancelled"},
        headers=_admin(restaurant_a),
    )

    assert cancelled.json()["status"] == "cancelled"
    assert reopened.status_code == 201
    assert reopened.json()["number"] == 2
    assert [c["number"] for c in listed.json()["items"]] == [1]


async def test_counts_belong_to_their_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco")
    count = (
        await client.post(f"{INVENTORY_URL}/stock-counts", json={}, headers=_admin(restaurant_a))
    ).json()
    count_url = f"{INVENTORY_URL}/stock-counts/{count['id']}"
    foreign = _admin(restaurant_b)

    responses = [
        await client.get(count_url, headers=foreign),
        await client.put(
            f"{count_url}/lines",
            json={"lines": [{"ingredient_id": liquor["id"], "quantity": "1"}]},
            headers=foreign,
        ),
        await client.post(f"{count_url}/close", headers=foreign),
        await client.post(f"{count_url}/cancel", headers=foreign),
        await client.get(f"{count_url}/differences", headers=foreign),
        await client.get(
            f"{INVENTORY_URL}/ingredients/{liquor['id']}/count-differences", headers=foreign
        ),
        await client.post(
            f"{INVENTORY_URL}/stock-counts",
            json={"scope": "selected", "ingredient_ids": [liquor["id"]]},
            headers=foreign,
        ),
    ]
    listed = await client.get(f"{INVENTORY_URL}/stock-counts", headers=foreign)

    assert [r.status_code for r in responses] == [404] * 7
    assert listed.json()["total"] == 0


async def test_waiter_cannot_count_and_a_counter_role_can(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco")
    waiter = authorization_for(restaurant_a.waiter)
    reader = authorization_for(
        await staff_with_role(session, restaurant_a, "Almacén", {Permission.INVENTORY_READ.value})
    )

    denied_open = await client.post(f"{INVENTORY_URL}/stock-counts", json={}, headers=waiter)
    denied_list = await client.get(f"{INVENTORY_URL}/stock-counts", headers=waiter)
    count = (
        await client.post(f"{INVENTORY_URL}/stock-counts", json={}, headers=_admin(restaurant_a))
    ).json()
    read = await client.get(f"{INVENTORY_URL}/stock-counts/{count['id']}", headers=reader)
    denied_lines = await client.put(
        f"{INVENTORY_URL}/stock-counts/{count['id']}/lines",
        json={"lines": [{"ingredient_id": liquor["id"], "quantity": "1"}]},
        headers=reader,
    )

    assert (denied_open.status_code, denied_list.status_code) == (403, 403)
    assert read.status_code == 200
    assert denied_lines.status_code == 403


# -- Mermas por aprobar -------------------------------------------------------------------


async def test_big_waste_waits_for_approval_and_moves_stock_only_when_approved(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco")
    await _buy(client, restaurant_a, liquor, "3000")
    headers = _admin(restaurant_a)

    small = await client.post(
        f"{INVENTORY_URL}/waste",
        json={"ingredient_id": liquor["id"], "quantity": "100", "reason": "Se derramó"},
        headers=headers,
    )
    big = await client.post(
        f"{INVENTORY_URL}/waste",
        json={"ingredient_id": liquor["id"], "quantity": "1500", "reason": "Se rompió la caja"},
        headers=headers,
    )

    assert small.status_code == 201
    assert big.status_code == 202, big.text
    pending = big.json()
    assert (
        pending["status"],
        pending["value"],
        pending["quantity"],
        pending["ingredient_name"],
        pending["unit"],
        pending["created_by_name"],
        pending["decided_by_name"],
    ) == (
        "pending_approval",
        "60.00",
        "1500.000",
        "Pisco",
        "ml",
        "Rosa Pérez",
        None,
    )
    assert await _stock(client, restaurant_a, liquor["id"]) == "2900.000"

    listed = await client.get(
        f"{INVENTORY_URL}/waste-approvals", params={"status": "pending_approval"}, headers=headers
    )
    assert [(a["id"], a["created_by_name"]) for a in listed.json()["items"]] == [
        (pending["id"], "Rosa Pérez")
    ]

    approved = await client.post(
        f"{INVENTORY_URL}/waste-approvals/{pending['id']}/approve", headers=headers
    )
    twice = await client.post(
        f"{INVENTORY_URL}/waste-approvals/{pending['id']}/approve", headers=headers
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["decided_by_name"] == "Rosa Pérez"
    assert approved.json()["movement_id"] is not None
    assert twice.status_code == 409
    assert twice.json()["code"] == "waste_already_decided"
    assert await _stock(client, restaurant_a, liquor["id"]) == "1400.000"


async def test_rejected_waste_leaves_stock_and_threshold_is_configurable(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco")
    await _buy(client, restaurant_a, liquor, "3000")
    headers = _admin(restaurant_a)
    restaurant = await client.get(url("restaurant"), headers=headers)
    assert restaurant.json()["waste_approval_threshold"] == "50.00"

    lowered = await client.patch(
        url("restaurant"), json={"waste_approval_threshold": "1"}, headers=headers
    )
    pending = await client.post(
        f"{INVENTORY_URL}/waste",
        json={"ingredient_id": liquor["id"], "quantity": "100", "reason": "Se derramó"},
        headers=headers,
    )
    no_reason = await client.post(
        f"{INVENTORY_URL}/waste-approvals/{pending.json()['id']}/reject",
        json={"reason": "   "},
        headers=headers,
    )
    rejected = await client.post(
        f"{INVENTORY_URL}/waste-approvals/{pending.json()['id']}/reject",
        json={"reason": "No cuadra con la cámara"},
        headers=headers,
    )
    disabled = await client.patch(
        url("restaurant"), json={"waste_approval_threshold": None}, headers=headers
    )
    untouched = await client.patch(url("restaurant"), json={"name": "Otro nombre"}, headers=headers)
    direct = await client.post(
        f"{INVENTORY_URL}/waste",
        json={"ingredient_id": liquor["id"], "quantity": "2900", "reason": "Inundación"},
        headers=headers,
    )

    assert lowered.json()["waste_approval_threshold"] == "1.00"
    assert pending.status_code == 202
    assert no_reason.status_code == 422
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rejection_reason"] == "No cuadra con la cámara"
    assert disabled.json()["waste_approval_threshold"] is None
    assert untouched.json()["waste_approval_threshold"] is None
    assert direct.status_code == 201
    assert await _stock(client, restaurant_a, liquor["id"]) == "100.000"


async def test_only_approvers_decide_and_only_in_their_restaurant(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    liquor = await _ingredient(client, restaurant_a, "Pisco")
    await _buy(client, restaurant_a, liquor, "3000")
    keeper = authorization_for(
        await staff_with_role(session, restaurant_a, "Almacén", {Permission.INVENTORY_MANAGE.value})
    )
    approver = authorization_for(
        await staff_with_role(
            session, restaurant_a, "Supervisor", {Permission.INVENTORY_APPROVE_WASTE.value}
        )
    )
    pending = await client.post(
        f"{INVENTORY_URL}/waste",
        json={"ingredient_id": liquor["id"], "quantity": "1500", "reason": "Se rompió"},
        headers=keeper,
    )
    approve_url = f"{INVENTORY_URL}/waste-approvals/{pending.json()['id']}/approve"

    by_keeper = await client.post(approve_url, headers=keeper)
    by_waiter = await client.post(approve_url, headers=authorization_for(restaurant_a.waiter))
    by_foreign = await client.post(approve_url, headers=_admin(restaurant_b))
    foreign_list = await client.get(
        f"{INVENTORY_URL}/waste-approvals", headers=_admin(restaurant_b)
    )
    approver_list = await client.get(f"{INVENTORY_URL}/waste-approvals", headers=approver)
    by_approver = await client.post(approve_url, headers=approver)

    assert pending.status_code == 202
    assert (by_keeper.status_code, by_waiter.status_code, by_foreign.status_code) == (
        403,
        403,
        404,
    )
    assert foreign_list.json()["total"] == 0
    assert approver_list.json()["total"] == 1
    assert by_approver.status_code == 200
    assert await _stock(client, restaurant_a, liquor["id"]) == "1500.000"
