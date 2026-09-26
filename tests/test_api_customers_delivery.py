"""Clientes frecuentes, delivery y pedidos que no se duplican al reintentar."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import Carta, caja_abierta, carta
from tests.conftest import StaffedRestaurant, authorization_for

CUSTOMERS_URL = "/api/v1/customers"
ORDERS_URL = "/api/v1/orders"


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    menu = await carta(session, local_a.id)
    await caja_abierta(session, local_a.id, local_a.admin.id or 0)
    return menu


async def _cliente(client: AsyncClient, local: StaffedRestaurant, **datos: Any) -> dict[str, Any]:
    body = {"name": "Ana Torres", "phone": "987 654 321", "address": "Jr. Pizarro 450", **datos}
    response = await client.post(CUSTOMERS_URL, json=body, headers=authorization_for(local.waiter))
    assert response.status_code == 201, response.text
    return response.json()


async def test_el_telefono_no_se_repite_y_se_busca_sin_espacios(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    await _cliente(client, local_a)

    repetido = await client.post(
        CUSTOMERS_URL,
        json={"name": "Otra", "phone": "987654321"},
        headers=authorization_for(local_a.waiter),
    )
    # Otro local puede tener a alguien con el mismo número.
    en_otro_local = await client.post(
        CUSTOMERS_URL,
        json={"name": "Ana", "phone": "987654321"},
        headers=authorization_for(local_b.admin),
    )
    busqueda = await client.get(
        CUSTOMERS_URL, params={"q": "987654"}, headers=authorization_for(local_a.waiter)
    )

    assert repetido.status_code == 409
    assert en_otro_local.status_code == 201
    assert [c["name"] for c in busqueda.json()["items"]] == ["Ana Torres"]


async def test_un_delivery_exige_telefono_y_direccion(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    sin_direccion = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "customer_name": "Luis",
            "customer_phone": "999111222",
            "items": [{"menu_item_id": carta_a.lomo}],
        },
        headers=authorization_for(local_a.waiter),
    )
    con_mesa = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "table_id": carta_a.mesa_1,
            "customer_name": "Luis",
            "customer_phone": "999111222",
            "delivery_address": "Av. España 100",
        },
        headers=authorization_for(local_a.waiter),
    )

    assert sin_direccion.status_code == 422
    assert "dirección" in sin_direccion.json()["detail"]
    assert con_mesa.status_code == 422


async def test_el_cliente_de_la_libreta_completa_el_delivery_y_suma_visitas(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    cliente = await _cliente(client, local_a, reference="Puerta verde", notes="Alérgica al maní")
    mesero, encargado = authorization_for(local_a.waiter), authorization_for(local_a.admin)

    pedido = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "customer_id": cliente["id"],
            "items": [{"menu_item_id": carta_a.lomo, "quantity": 2}],
        },
        headers=mesero,
    )
    order = pedido.json()
    for paso, quien in (("send", mesero), ("ready", encargado), ("served", mesero)):
        await client.post(f"{ORDERS_URL}/{order['id']}/{paso}", headers=quien)
    await client.post(
        f"{ORDERS_URL}/{order['id']}/charge", json={"payment_method": "yape"}, headers=mesero
    )
    ficha = await client.get(f"{CUSTOMERS_URL}/{cliente['id']}", headers=mesero)

    assert pedido.status_code == 201, pedido.text
    assert order["type_label"] == "Delivery"
    assert (order["customer_name"], order["customer_phone"]) == ("Ana Torres", "987 654 321")
    assert order["delivery_address"] == "Jr. Pizarro 450"
    assert order["delivery_reference"] == "Puerta verde"
    body = ficha.json()
    assert (body["visits"], body["spent"], body["is_frequent"]) == (1, "56.00", False)
    assert body["recent_orders"][0]["order_id"] == order["id"]


async def test_un_telefono_de_la_libreta_identifica_al_cliente_sin_buscarlo(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    cliente = await _cliente(client, local_a)

    pedido = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "customer_name": "Ana",
            "customer_phone": "987654321",
            "delivery_address": "Otra dirección 12",
            "items": [{"menu_item_id": carta_a.lomo}],
        },
        headers=authorization_for(local_a.waiter),
    )

    assert pedido.status_code == 201, pedido.text
    assert pedido.json()["customer_id"] == cliente["id"]
    # Lo que escribió el mesero vale: hoy lo pidió a otra dirección.
    assert pedido.json()["delivery_address"] == "Otra dirección 12"


async def test_un_cliente_de_otro_local_no_sirve(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant, carta_a: Carta
) -> None:
    ajeno = await client.post(
        CUSTOMERS_URL, json={"name": "Ana"}, headers=authorization_for(local_b.admin)
    )

    response = await client.post(
        ORDERS_URL,
        json={"type": "takeaway", "customer_id": ajeno.json()["id"]},
        headers=authorization_for(local_a.waiter),
    )
    ficha = await client.get(
        f"{CUSTOMERS_URL}/{ajeno.json()['id']}", headers=authorization_for(local_a.admin)
    )

    assert response.status_code == 404
    assert ficha.status_code == 404


async def test_el_reintento_del_celular_no_duplica_el_pedido(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    body = {
        "type": "takeaway",
        "client_request_id": "cel-0001-abcdef",
        "items": [{"menu_item_id": carta_a.aji}],
    }

    primero = await client.post(ORDERS_URL, json=body, headers=authorization_for(local_a.waiter))
    reintento = await client.post(ORDERS_URL, json=body, headers=authorization_for(local_a.waiter))
    activos = await client.get(f"{ORDERS_URL}/active", headers=authorization_for(local_a.admin))

    assert primero.json()["id"] == reintento.json()["id"]
    assert len(activos.json()) == 1
