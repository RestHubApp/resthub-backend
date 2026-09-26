"""Proveedores, órdenes de compra y sugerencias de compra, por HTTP."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import StaffedRestaurant, authorization_for

INVENTORY_URL = "/api/v1/inventory"


def _admin(local: StaffedRestaurant) -> dict[str, str]:
    return authorization_for(local.admin)


async def _insumo(client: AsyncClient, local: StaffedRestaurant, nombre: str, minimo: str) -> int:
    response = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": nombre, "unit": "g", "min_stock": minimo, "unit_cost": "0.010"},
        headers=_admin(local),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _proveedor(client: AsyncClient, local: StaffedRestaurant, nombre: str) -> int:
    response = await client.post(
        f"{INVENTORY_URL}/suppliers",
        json={"name": nombre, "contact": "Don Pedro", "phone": "987654321"},
        headers=_admin(local),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _orden(
    client: AsyncClient, local: StaffedRestaurant, supplier_id: int, *lines: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        f"{INVENTORY_URL}/purchase-orders",
        json={"supplier_id": supplier_id, "lines": list(lines), "notes": "Para el viernes"},
        headers=_admin(local),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_proveedores_con_nombre_unico(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    await _proveedor(client, local_a, "Mercado Mayorista")

    repetido = await client.post(
        f"{INVENTORY_URL}/suppliers", json={"name": "mercado mayorista"}, headers=_admin(local_a)
    )
    del_mesero = await client.post(
        f"{INVENTORY_URL}/suppliers",
        json={"name": "Otro"},
        headers=authorization_for(local_a.waiter),
    )

    assert repetido.status_code == 409
    assert del_mesero.status_code == 403


async def test_la_orden_recibida_entra_al_stock_con_el_costo_real(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    arroz = await _insumo(client, local_a, "Arroz", "1000")
    pollo = await _insumo(client, local_a, "Pollo", "1000")
    proveedor = await _proveedor(client, local_a, "Mercado Mayorista")
    orden = await _orden(
        client,
        local_a,
        proveedor,
        {"ingredient_id": arroz, "quantity": "5000", "unit_cost": "0.004"},
        {"ingredient_id": pollo, "quantity": "3000", "unit_cost": "0.012"},
    )
    lineas = {line["ingredient_id"]: line["id"] for line in orden["lines"]}

    enviada = await client.post(
        f"{INVENTORY_URL}/purchase-orders/{orden['id']}/send", headers=_admin(local_a)
    )
    recibida = await client.post(
        f"{INVENTORY_URL}/purchase-orders/{orden['id']}/receive",
        # Llegó todo el arroz a otro precio y del pollo, solo 2 kg.
        json={
            "lines": [
                {"line_id": lineas[arroz], "quantity": "5000", "unit_cost": "0.0045"},
                {"line_id": lineas[pollo], "quantity": "2000", "unit_cost": "0.012"},
            ]
        },
        headers=_admin(local_a),
    )
    otra_vez = await client.post(
        f"{INVENTORY_URL}/purchase-orders/{orden['id']}/receive",
        json={"lines": []},
        headers=_admin(local_a),
    )
    insumos = await client.get(f"{INVENTORY_URL}/ingredients", headers=_admin(local_a))
    movimientos = await client.get(
        f"{INVENTORY_URL}/movements", params={"kind": "purchase"}, headers=_admin(local_a)
    )

    assert orden["number"] == 1
    assert orden["estimated_total"] == "56.00"
    assert enviada.json()["status"] == "sent"
    body = recibida.json()
    assert body["status"] == "received"
    assert body["received_total"] == "46.50"
    assert otra_vez.status_code == 422
    stock = {row["name"]: row["stock"] for row in insumos.json()}
    assert stock == {"Arroz": "5000.000", "Pollo": "2000.000"}
    assert {m["reason"] for m in movimientos.json()["items"]} == {"OC 1 · Mercado Mayorista"}


async def test_solo_el_borrador_se_edita_y_lo_recibido_no_se_cancela(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    arroz = await _insumo(client, local_a, "Arroz", "1000")
    proveedor = await _proveedor(client, local_a, "Mercado")
    orden = await _orden(
        client,
        local_a,
        proveedor,
        {"ingredient_id": arroz, "quantity": "1000", "unit_cost": "0.004"},
    )
    url = f"{INVENTORY_URL}/purchase-orders/{orden['id']}"

    editada = await client.put(
        url,
        json={"lines": [{"ingredient_id": arroz, "quantity": "2500", "unit_cost": "0.004"}]},
        headers=_admin(local_a),
    )
    await client.post(f"{url}/send", headers=_admin(local_a))
    tarde = await client.put(
        url,
        json={"lines": [{"ingredient_id": arroz, "quantity": "1", "unit_cost": "0"}]},
        headers=_admin(local_a),
    )
    await client.post(f"{url}/receive", json={"lines": []}, headers=_admin(local_a))
    cancelar = await client.post(f"{url}/cancel", headers=_admin(local_a))

    assert editada.json()["lines"][0]["quantity"] == "2500.000"
    assert tarde.status_code == 422
    assert cancelar.status_code == 422


async def test_una_orden_no_repite_insumos(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    arroz = await _insumo(client, local_a, "Arroz", "1000")
    proveedor = await _proveedor(client, local_a, "Mercado")
    linea = {"ingredient_id": arroz, "quantity": "1000", "unit_cost": "0.004"}

    response = await client.post(
        f"{INVENTORY_URL}/purchase-orders",
        json={"supplier_id": proveedor, "lines": [linea, linea]},
        headers=_admin(local_a),
    )

    assert response.status_code == 422


async def test_las_sugerencias_cubren_el_doble_del_minimo(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    bajo = await _insumo(client, local_a, "Aceite", "2000")
    sobrado = await _insumo(client, local_a, "Sal", "100")
    await client.post(
        f"{INVENTORY_URL}/purchases",
        json={"ingredient_id": bajo, "quantity": "500", "unit_cost": "0.01"},
        headers=_admin(local_a),
    )
    await client.post(
        f"{INVENTORY_URL}/purchases",
        json={"ingredient_id": sobrado, "quantity": "5000", "unit_cost": "0.001"},
        headers=_admin(local_a),
    )

    response = await client.get(f"{INVENTORY_URL}/purchase-suggestions", headers=_admin(local_a))

    sugerencias = response.json()
    assert [s["ingredient_name"] for s in sugerencias] == ["Aceite"]
    # Tiene 500 y el mínimo es 2000: se piden 3500 para llegar a 4000.
    assert sugerencias[0]["quantity"] == "3500.000"


async def test_un_local_no_ve_las_ordenes_de_otro(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    arroz = await _insumo(client, local_b, "Arroz", "1000")
    proveedor = await _proveedor(client, local_b, "Mercado")
    ajena = await _orden(
        client, local_b, proveedor, {"ingredient_id": arroz, "quantity": "1", "unit_cost": "1"}
    )

    leer = await client.get(
        f"{INVENTORY_URL}/purchase-orders/{ajena['id']}", headers=_admin(local_a)
    )
    recibir = await client.post(
        f"{INVENTORY_URL}/purchase-orders/{ajena['id']}/receive",
        json={"lines": []},
        headers=_admin(local_a),
    )
    con_proveedor_ajeno = await client.post(
        f"{INVENTORY_URL}/purchase-orders",
        json={
            "supplier_id": proveedor,
            "lines": [{"ingredient_id": arroz, "quantity": "1", "unit_cost": "1"}],
        },
        headers=_admin(local_a),
    )

    assert leer.status_code == 404
    assert recibir.status_code == 404
    assert con_proveedor_ajeno.status_code == 404
