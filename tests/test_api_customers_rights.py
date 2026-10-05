"""Derechos ARCO del cliente por HTTP (Ley N.º 29733): acceso y cancelación."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import Carta, caja_abierta, carta
from tests.conftest import StaffedRestaurant, authorization_for

CUSTOMERS_URL = "/api/v1/customers"
ORDERS_URL = "/api/v1/orders"
RESERVATIONS_URL = "/api/v1/reservations"
ACTIVITY_URL = "/api/v1/activity"


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    menu = await carta(session, local_a.id)
    await caja_abierta(session, local_a.id, local_a.admin.id or 0)
    return menu


async def _cliente(client: AsyncClient, local: StaffedRestaurant) -> dict[str, Any]:
    response = await client.post(
        CUSTOMERS_URL,
        json={
            "name": "Ana Torres",
            "phone": "987 654 321",
            "address": "Jr. Pizarro 450",
            "notes": "Alérgica al maní",
            "consent": True,
        },
        headers=authorization_for(local.admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _delivery(
    client: AsyncClient, local: StaffedRestaurant, menu: Carta, cliente_id: int, cobrar: bool
) -> dict[str, Any]:
    mesero, encargado = authorization_for(local.waiter), authorization_for(local.admin)
    creado = await client.post(
        ORDERS_URL,
        json={
            "type": "delivery",
            "customer_id": cliente_id,
            "notes": "Tocar el timbre dos veces",
            "items": [{"menu_item_id": menu.lomo, "quantity": 1, "notes": "sin maní"}],
        },
        headers=mesero,
    )
    assert creado.status_code == 201, creado.text
    pedido = creado.json()
    if cobrar:
        for paso, quien in (("send", mesero), ("ready", encargado), ("served", mesero)):
            await client.post(f"{ORDERS_URL}/{pedido['id']}/{paso}", headers=quien)
        cobro = await client.post(
            f"{ORDERS_URL}/{pedido['id']}/charge", json={"payment_method": "yape"}, headers=mesero
        )
        assert cobro.status_code == 200, cobro.text
    return pedido


async def _reserva(client: AsyncClient, local: StaffedRestaurant, cliente_id: int) -> None:
    dia = (datetime.now(UTC) - timedelta(hours=5) + timedelta(days=1)).date()
    response = await client.post(
        RESERVATIONS_URL,
        json={
            "customer_name": "Ana Torres",
            "phone": "987654321",
            "party_size": 4,
            "reserved_for": f"{dia.isoformat()}T20:00:00-05:00",
            "customer_id": cliente_id,
            "notes": "Cumpleaños",
        },
        headers=authorization_for(local.waiter),
    )
    assert response.status_code == 201, response.text


async def test_el_encargado_exporta_todo_lo_que_se_guarda_del_cliente(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    cliente = await _cliente(client, local_a)
    await _delivery(client, local_a, carta_a, cliente["id"], cobrar=True)
    await _reserva(client, local_a, cliente["id"])
    encargado = authorization_for(local_a.admin)

    exportado = await client.get(f"{CUSTOMERS_URL}/{cliente['id']}/export", headers=encargado)
    bitacora = await client.get(ACTIVITY_URL, headers=encargado)

    assert exportado.status_code == 200, exportado.text
    datos = exportado.json()
    assert (datos["name"], datos["notes"]) == ("Ana Torres", "Alérgica al maní")
    assert datos["consent_at"] is not None
    assert [p["delivery_address"] for p in datos["orders"]] == ["Jr. Pizarro 450"]
    assert datos["orders"][0]["notes"] == "Tocar el timbre dos veces"
    assert [r["notes"] for r in datos["reservations"]] == ["Cumpleaños"]
    # La bitácora anota el pedido sin repetir el nombre.
    assert any(
        e["kind"] == "customer_exported" and e["detail"] == f"Cliente #{cliente['id']}"
        for e in bitacora.json()["items"]
    )


async def test_borrar_quita_sus_datos_de_la_ficha_los_pedidos_y_las_reservas(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    cliente = await _cliente(client, local_a)
    pedido = await _delivery(client, local_a, carta_a, cliente["id"], cobrar=True)
    await _reserva(client, local_a, cliente["id"])
    encargado = authorization_for(local_a.admin)

    borrado = await client.post(f"{CUSTOMERS_URL}/{cliente['id']}/anonymize", headers=encargado)
    ficha = await client.get(f"{CUSTOMERS_URL}/{cliente['id']}", headers=encargado)
    libreta = await client.get(CUSTOMERS_URL, headers=encargado)
    detalle = (await client.get(f"{ORDERS_URL}/{pedido['id']}", headers=encargado)).json()
    reservas = await client.get(RESERVATIONS_URL, headers=encargado, params={"day": _manana()})
    # El teléfono queda libre: otra persona puede usarlo.
    otra = await client.post(
        CUSTOMERS_URL,
        json={"name": "Otra persona", "phone": "987654321", "consent": True},
        headers=encargado,
    )

    assert borrado.status_code == 204, borrado.text
    assert ficha.status_code == 404
    assert [c["name"] for c in libreta.json()["items"]] == []
    assert detalle["customer_name"] == "Cliente eliminado"
    assert (detalle["customer_phone"], detalle["delivery_address"], detalle["notes"]) == (
        "",
        "",
        "",
    )
    assert all(item["notes"] == "" for item in detalle["items"])
    # Sus ventas siguen contando.
    assert detalle["status"] == "paid"
    assert [(r["customer_name"], r["phone"], r["notes"]) for r in reservas.json()] == [
        ("Cliente eliminado", "", "")
    ]
    assert otra.status_code == 201, otra.text


def _manana() -> str:
    return (datetime.now(UTC) - timedelta(hours=5) + timedelta(days=1)).date().isoformat()


async def test_con_pedidos_en_curso_no_se_borra(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    # La cocina todavía necesita sus notas (una alergia, por ejemplo).
    cliente = await _cliente(client, local_a)
    await _delivery(client, local_a, carta_a, cliente["id"], cobrar=False)

    borrado = await client.post(
        f"{CUSTOMERS_URL}/{cliente['id']}/anonymize", headers=authorization_for(local_a.admin)
    )
    ficha = await client.get(
        f"{CUSTOMERS_URL}/{cliente['id']}", headers=authorization_for(local_a.admin)
    )

    assert borrado.status_code == 409
    assert "pedidos en curso" in borrado.json()["detail"]
    assert ficha.json()["name"] == "Ana Torres"


async def test_el_mesero_no_exporta_ni_borra_y_otro_local_no_ve_al_cliente(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    cliente = await _cliente(client, local_a)
    mesero = authorization_for(local_a.waiter)
    ajeno = authorization_for(local_b.admin)

    respuestas = [
        await client.get(f"{CUSTOMERS_URL}/{cliente['id']}/export", headers=mesero),
        await client.post(f"{CUSTOMERS_URL}/{cliente['id']}/anonymize", headers=mesero),
        await client.get(f"{CUSTOMERS_URL}/{cliente['id']}/export", headers=ajeno),
        await client.post(f"{CUSTOMERS_URL}/{cliente['id']}/anonymize", headers=ajeno),
    ]

    assert [r.status_code for r in respuestas] == [403, 403, 404, 404]
