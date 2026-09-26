"""Cambiar un pedido de mesa y unir dos mesas por HTTP."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.orders.adapters.persistence.sqlalchemy_table_repository import (
    SqlAlchemyTableRepository,
)
from resthub.modules.orders.domain.tables import DiningTable
from tests.builders import Carta, carta
from tests.conftest import StaffedRestaurant, authorization_for

ORDERS_URL = "/api/v1/orders"


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    return await carta(session, local_a.id)


async def _mesa(
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


async def _paso(
    client: AsyncClient, headers: dict[str, str], order_id: int, paso: str
) -> dict[str, Any]:
    response = await client.post(f"{ORDERS_URL}/{order_id}/{paso}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_el_pedido_se_muda_a_una_mesa_libre(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero = authorization_for(local_a.waiter)
    pedido = await _mesa(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))

    movido = await client.post(
        f"{ORDERS_URL}/{pedido['id']}/move", json={"table_id": carta_a.mesa_2}, headers=mesero
    )
    mesas = await client.get("/api/v1/tables", headers=mesero)
    bitacora = await client.get(
        "/api/v1/activity", params={"kind": "order_moved"}, headers=authorization_for(local_a.admin)
    )

    assert movido.status_code == 200
    assert movido.json()["table_label"] == "2"
    estados = {mesa["label"]: mesa["status"] for mesa in mesas.json()}
    assert estados == {"1": "free", "2": "occupied"}
    assert bitacora.json()["items"][0]["detail"] == f"Pedido #{pedido['number']}: de 1 a 2"


async def test_no_se_muda_a_una_mesa_ocupada_ni_inactiva(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero = authorization_for(local_a.waiter)
    inactiva = await SqlAlchemyTableRepository(session).add(
        DiningTable(restaurant_id=local_a.id, label="Terraza", is_active=False)
    )
    await session.commit()
    uno = await _mesa(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))
    await _mesa(client, mesero, carta_a.mesa_2, (carta_a.aji, 1))
    url = f"{ORDERS_URL}/{uno['id']}/move"

    ocupada = await client.post(url, json={"table_id": carta_a.mesa_2}, headers=mesero)
    apagada = await client.post(url, json={"table_id": inactiva.id}, headers=mesero)

    assert ocupada.status_code == 409
    assert apagada.status_code == 409


async def test_unir_mesas_junta_los_platos_y_libera_la_otra(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero, encargado = authorization_for(local_a.waiter), authorization_for(local_a.admin)
    await caja_abierta_http(client, local_a)
    uno = await _mesa(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))
    dos = await _mesa(client, mesero, carta_a.mesa_2, (carta_a.aji, 2), notes="Sin ají")
    # La mesa 1 ya comió: está servida. La 2 sigue abierta.
    for paso, quien in (("send", mesero), ("ready", encargado), ("served", mesero)):
        await _paso(client, quien, uno["id"], paso)

    unido = await client.post(
        f"{ORDERS_URL}/{uno['id']}/merge", json={"source_order_id": dos["id"]}, headers=mesero
    )
    cerrado = await client.get(f"{ORDERS_URL}/{dos['id']}", headers=encargado)
    mesas = await client.get("/api/v1/tables", headers=mesero)
    resumen = await client.get("/api/v1/insights/summary", headers=encargado)

    body = unido.json()
    assert unido.status_code == 200
    assert body["total"] == "72.00"
    assert body["item_count"] == 3
    # Quedó abierto: los platos de la mesa 2 todavía no fueron a cocina.
    assert body["status"] == "open"
    assert body["notes"] == "Sin ají"
    assert cerrado.json()["status"] == "cancelled"
    assert cerrado.json()["merged_into_id"] == uno["id"]
    assert cerrado.json()["cancel_reason"] == f"Unido al pedido #{uno['number']}"
    estados = {mesa["label"]: mesa["status"] for mesa in mesas.json()}
    assert estados == {"1": "occupied", "2": "free"}
    # Una mesa unida no cuenta como pedido cancelado en el panel.
    assert resumen.json()["cancelled_orders"] == 0


async def test_no_se_unen_mesas_con_pagos(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero, encargado = authorization_for(local_a.waiter), authorization_for(local_a.admin)
    await caja_abierta_http(client, local_a)
    uno = await _mesa(client, mesero, carta_a.mesa_1, (carta_a.lomo, 2))
    dos = await _mesa(client, mesero, carta_a.mesa_2, (carta_a.aji, 1))
    for paso, quien in (("send", mesero), ("ready", encargado), ("served", mesero)):
        await _paso(client, quien, uno["id"], paso)
    parte = await client.post(
        f"{ORDERS_URL}/{uno['id']}/payments",
        json={"payment_method": "yape", "amount": "10.00", "expected_balance": uno["total"]},
        headers=mesero,
    )
    assert parte.status_code == 201

    response = await client.post(
        f"{ORDERS_URL}/{dos['id']}/merge", json={"source_order_id": uno["id"]}, headers=mesero
    )
    consigo = await client.post(
        f"{ORDERS_URL}/{dos['id']}/merge", json={"source_order_id": dos["id"]}, headers=mesero
    )

    assert response.status_code == 409
    assert consigo.status_code == 422


async def test_el_plato_ya_servido_no_se_descuenta_dos_veces_al_unir(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero, encargado = authorization_for(local_a.waiter), authorization_for(local_a.admin)
    insumo = await client.post(
        "/api/v1/inventory/ingredients",
        json={"name": "Lomo fino", "unit": "g", "min_stock": "0", "unit_cost": "0.05"},
        headers=encargado,
    )
    ingredient_id = insumo.json()["id"]
    await client.put(
        f"/api/v1/inventory/recipes/{carta_a.lomo}",
        json={"lines": [{"ingredient_id": ingredient_id, "quantity": "200"}]},
        headers=encargado,
    )
    await client.post(
        "/api/v1/inventory/purchases",
        json={"ingredient_id": ingredient_id, "quantity": "1000", "unit_cost": "0.05"},
        headers=encargado,
    )
    uno = await _mesa(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))
    dos = await _mesa(client, mesero, carta_a.mesa_2, (carta_a.lomo, 1))
    for pedido in (uno, dos):
        for paso, quien in (("send", mesero), ("ready", encargado), ("served", mesero)):
            await _paso(client, quien, pedido["id"], paso)

    await client.post(
        f"{ORDERS_URL}/{uno['id']}/merge", json={"source_order_id": dos["id"]}, headers=mesero
    )
    await client.post(
        f"{ORDERS_URL}/{uno['id']}/items",
        json={"items": [{"menu_item_id": carta_a.aji}]},
        headers=mesero,
    )
    for paso, quien in (("ready", encargado), ("served", mesero)):
        await _paso(client, quien, uno["id"], paso)
    movimientos = await client.get(
        "/api/v1/inventory/movements",
        params={"ingredient_id": ingredient_id, "kind": "consumption"},
        headers=encargado,
    )

    # Dos lomos servidos, dos consumos: unir y volver a servir no descuenta de nuevo.
    assert movimientos.json()["total"] == 2


async def caja_abierta_http(client: AsyncClient, local: StaffedRestaurant) -> None:
    response = await client.post(
        "/api/v1/cash/open", json={"opening_amount": "0"}, headers=authorization_for(local.admin)
    )
    assert response.status_code == 201, response.text
