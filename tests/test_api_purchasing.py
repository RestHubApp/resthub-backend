"""Proveedores, órdenes de compra y sugerencias de compra, por HTTP."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import StaffedRestaurant, authorization_for, url

INVENTORY_URL = url("inventory")


def _admin(restaurant: StaffedRestaurant) -> dict[str, str]:
    return authorization_for(restaurant.admin)


async def _ingredient(
    client: AsyncClient, restaurant: StaffedRestaurant, name: str, minimum: str
) -> int:
    response = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": name, "unit": "g", "min_stock": minimum, "unit_cost": "0.010"},
        headers=_admin(restaurant),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _supplier(client: AsyncClient, restaurant: StaffedRestaurant, name: str) -> int:
    response = await client.post(
        f"{INVENTORY_URL}/suppliers",
        json={"name": name, "contact": "Don Pedro", "phone": "987654321"},
        headers=_admin(restaurant),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _order(
    client: AsyncClient, restaurant: StaffedRestaurant, supplier_id: int, *lines: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        f"{INVENTORY_URL}/purchase-orders",
        json={"supplier_id": supplier_id, "lines": list(lines), "notes": "Para el viernes"},
        headers=_admin(restaurant),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_suppliers_have_unique_names(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _supplier(client, restaurant_a, "Mercado Mayorista")

    repeated = await client.post(
        f"{INVENTORY_URL}/suppliers",
        json={"name": "mercado mayorista"},
        headers=_admin(restaurant_a),
    )
    by_waiter = await client.post(
        f"{INVENTORY_URL}/suppliers",
        json={"name": "Otro"},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert repeated.status_code == 409
    assert by_waiter.status_code == 403


async def test_received_order_enters_stock_at_real_cost(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    arroz = await _ingredient(client, restaurant_a, "Arroz", "1000")
    chicken = await _ingredient(client, restaurant_a, "Pollo", "1000")
    supplier = await _supplier(client, restaurant_a, "Mercado Mayorista")
    order = await _order(
        client,
        restaurant_a,
        supplier,
        {"ingredient_id": arroz, "quantity": "5000", "unit_cost": "0.004"},
        {"ingredient_id": chicken, "quantity": "3000", "unit_cost": "0.012"},
    )
    line_ids = {line["ingredient_id"]: line["id"] for line in order["lines"]}

    sent = await client.post(
        f"{INVENTORY_URL}/purchase-orders/{order['id']}/send", headers=_admin(restaurant_a)
    )
    received = await client.post(
        f"{INVENTORY_URL}/purchase-orders/{order['id']}/receive",
        # Llegó todo el arroz a otro precio y del pollo, solo 2 kg.
        json={
            "lines": [
                {"line_id": line_ids[arroz], "quantity": "5000", "unit_cost": "0.0045"},
                {"line_id": line_ids[chicken], "quantity": "2000", "unit_cost": "0.012"},
            ]
        },
        headers=_admin(restaurant_a),
    )
    again = await client.post(
        f"{INVENTORY_URL}/purchase-orders/{order['id']}/receive",
        json={"lines": []},
        headers=_admin(restaurant_a),
    )
    ingredients = await client.get(f"{INVENTORY_URL}/ingredients", headers=_admin(restaurant_a))
    movements = await client.get(
        f"{INVENTORY_URL}/movements", params={"kind": "purchase"}, headers=_admin(restaurant_a)
    )

    assert order["number"] == 1
    assert order["estimated_total"] == "56.00"
    assert sent.json()["status"] == "sent"
    body = received.json()
    assert body["status"] == "received"
    assert body["received_total"] == "46.50"
    assert again.status_code == 422
    stock = {row["name"]: row["stock"] for row in ingredients.json()}
    assert stock == {"Arroz": "5000.000", "Pollo": "2000.000"}
    assert {m["reason"] for m in movements.json()["items"]} == {"OC 1 · Mercado Mayorista"}


async def test_only_draft_is_editable_and_received_cannot_be_cancelled(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    arroz = await _ingredient(client, restaurant_a, "Arroz", "1000")
    supplier = await _supplier(client, restaurant_a, "Mercado")
    order = await _order(
        client,
        restaurant_a,
        supplier,
        {"ingredient_id": arroz, "quantity": "1000", "unit_cost": "0.004"},
    )
    url = f"{INVENTORY_URL}/purchase-orders/{order['id']}"

    edited = await client.put(
        url,
        json={"lines": [{"ingredient_id": arroz, "quantity": "2500", "unit_cost": "0.004"}]},
        headers=_admin(restaurant_a),
    )
    await client.post(f"{url}/send", headers=_admin(restaurant_a))
    late = await client.put(
        url,
        json={"lines": [{"ingredient_id": arroz, "quantity": "1", "unit_cost": "0"}]},
        headers=_admin(restaurant_a),
    )
    await client.post(f"{url}/receive", json={"lines": []}, headers=_admin(restaurant_a))
    cancel = await client.post(f"{url}/cancel", headers=_admin(restaurant_a))

    assert edited.json()["lines"][0]["quantity"] == "2500.000"
    assert late.status_code == 422
    assert cancel.status_code == 422


async def test_order_has_no_repeated_ingredients(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    arroz = await _ingredient(client, restaurant_a, "Arroz", "1000")
    supplier = await _supplier(client, restaurant_a, "Mercado")
    line = {"ingredient_id": arroz, "quantity": "1000", "unit_cost": "0.004"}

    response = await client.post(
        f"{INVENTORY_URL}/purchase-orders",
        json={"supplier_id": supplier, "lines": [line, line]},
        headers=_admin(restaurant_a),
    )

    assert response.status_code == 422


async def test_suggestions_cover_double_the_minimum(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    low = await _ingredient(client, restaurant_a, "Aceite", "2000")
    surplus = await _ingredient(client, restaurant_a, "Sal", "100")
    await client.post(
        f"{INVENTORY_URL}/purchases",
        json={"ingredient_id": low, "quantity": "500", "unit_cost": "0.01"},
        headers=_admin(restaurant_a),
    )
    await client.post(
        f"{INVENTORY_URL}/purchases",
        json={"ingredient_id": surplus, "quantity": "5000", "unit_cost": "0.001"},
        headers=_admin(restaurant_a),
    )

    response = await client.get(
        f"{INVENTORY_URL}/purchase-suggestions", headers=_admin(restaurant_a)
    )

    suggestions = response.json()
    assert [s["ingredient_name"] for s in suggestions] == ["Aceite"]
    # Tiene 500 y el mínimo es 2000: se piden 3500 para llegar a 4000.
    assert suggestions[0]["quantity"] == "3500.000"


async def test_restaurant_cannot_see_another_restaurants_orders(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    arroz = await _ingredient(client, restaurant_b, "Arroz", "1000")
    supplier = await _supplier(client, restaurant_b, "Mercado")
    foreign_order = await _order(
        client, restaurant_b, supplier, {"ingredient_id": arroz, "quantity": "1", "unit_cost": "1"}
    )

    read = await client.get(
        f"{INVENTORY_URL}/purchase-orders/{foreign_order['id']}", headers=_admin(restaurant_a)
    )
    receive = await client.post(
        f"{INVENTORY_URL}/purchase-orders/{foreign_order['id']}/receive",
        json={"lines": []},
        headers=_admin(restaurant_a),
    )
    with_foreign_supplier = await client.post(
        f"{INVENTORY_URL}/purchase-orders",
        json={
            "supplier_id": supplier,
            "lines": [{"ingredient_id": arroz, "quantity": "1", "unit_cost": "1"}],
        },
        headers=_admin(restaurant_a),
    )

    assert read.status_code == 404
    assert receive.status_code == 404
    assert with_foreign_supplier.status_code == 404
