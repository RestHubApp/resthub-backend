"""Mesas por HTTP: estado libre/ocupada, permisos y aislamiento."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import carta
from tests.conftest import StaffedRestaurant, authorization_for

TABLES_URL = "/api/v1/tables"


async def test_crear_renombrar_ordenar_y_desactivar(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    admin = authorization_for(local_a.admin)
    uno = (await client.post(TABLES_URL, json={"label": "1"}, headers=admin)).json()
    barra = (await client.post(TABLES_URL, json={"label": "Barra"}, headers=admin)).json()

    repetida = await client.post(TABLES_URL, json={"label": "barra"}, headers=admin)
    renombrada = await client.patch(
        f"{TABLES_URL}/{uno['id']}", json={"label": "Terraza 1"}, headers=admin
    )
    ordenadas = await client.put(
        f"{TABLES_URL}/order", json={"ids": [barra["id"], uno["id"]]}, headers=admin
    )
    await client.patch(f"{TABLES_URL}/{barra['id']}", json={"is_active": False}, headers=admin)

    assert repetida.status_code == 409
    assert renombrada.json()["label"] == "Terraza 1"
    assert [t["label"] for t in ordenadas.json()] == ["Barra", "Terraza 1"]
    visibles = await client.get(TABLES_URL, headers=authorization_for(local_a.waiter))
    todas = await client.get(TABLES_URL, params={"include_inactive": True}, headers=admin)
    assert [t["label"] for t in visibles.json()] == ["Terraza 1"]
    assert len(todas.json()) == 2


async def test_la_mesa_muestra_su_pedido_activo(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    menu = await carta(session, local_a.id)
    mesero = authorization_for(local_a.waiter)
    order = await client.post(
        "/api/v1/orders",
        json={"type": "dine_in", "table_id": menu.mesa_2, "items": [{"menu_item_id": menu.lomo}]},
        headers=mesero,
    )

    response = await client.get(TABLES_URL, headers=mesero)

    libre, ocupada = response.json()
    assert (libre["status"], libre["status_label"], libre["active_order"]) == (
        "free",
        "Libre",
        None,
    )
    assert ocupada["status"] == "occupied"
    assert ocupada["active_order"]["id"] == order.json()["id"]
    assert ocupada["active_order"]["total"] == "28.00"
    assert ocupada["active_order"]["waiter_name"] == "Luis Torres"


async def test_una_mesa_desactivada_no_recibe_pedidos(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    menu = await carta(session, local_a.id)
    await client.patch(
        f"{TABLES_URL}/{menu.mesa_1}",
        json={"is_active": False},
        headers=authorization_for(local_a.admin),
    )

    response = await client.post(
        "/api/v1/orders",
        json={"type": "dine_in", "table_id": menu.mesa_1},
        headers=authorization_for(local_a.waiter),
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
async def test_el_mesero_no_administra_mesas(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    method: str,
    suffix: str,
    body: dict[str, object],
) -> None:
    response = await client.request(
        method, TABLES_URL + suffix, json=body, headers=authorization_for(local_a.waiter)
    )

    assert response.status_code == 403


async def test_un_local_no_ve_ni_toca_las_mesas_de_otro(
    client: AsyncClient,
    session: AsyncSession,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
) -> None:
    menu_b = await carta(session, local_b.id)
    admin_a = authorization_for(local_a.admin)

    editar = await client.patch(
        f"{TABLES_URL}/{menu_b.mesa_1}", json={"label": "X"}, headers=admin_a
    )
    listado = await client.get(TABLES_URL, headers=admin_a)
    ordenar = await client.put(
        f"{TABLES_URL}/order", json={"ids": [menu_b.mesa_1]}, headers=admin_a
    )

    assert editar.status_code == 404
    assert listado.json() == []
    assert ordenar.status_code == 422
