"""Inventario por HTTP: stock como suma del libro, costos, recetas, permisos y aislamiento."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import Carta, carta
from tests.conftest import StaffedRestaurant, authorization_for

INVENTORY_URL = "/api/v1/inventory"


async def _ingredient(
    client: AsyncClient,
    local: StaffedRestaurant,
    name: str,
    unit: str = "g",
    min_stock: str = "0",
    unit_cost: str = "0",
) -> dict[str, Any]:
    response = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": name, "unit": unit, "min_stock": min_stock, "unit_cost": unit_cost},
        headers=authorization_for(local.admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _post(
    client: AsyncClient, local: StaffedRestaurant, path: str, **body: Any
) -> dict[str, Any]:
    response = await client.post(
        f"{INVENTORY_URL}/{path}", json=body, headers=authorization_for(local.admin)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_el_stock_es_la_suma_del_libro(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    arroz = await _ingredient(client, local_a, "Arroz", min_stock="2000")
    assert arroz["stock"] == "0.000"

    await _post(
        client, local_a, "purchases", ingredient_id=arroz["id"], quantity="5000", unit_cost="0.004"
    )
    await _post(
        client,
        local_a,
        "waste",
        ingredient_id=arroz["id"],
        quantity="250.5",
        reason="Se mojó el saco",
    )
    ajuste = await _post(
        client, local_a, "adjustments", ingredient_id=arroz["id"], quantity="-49.5", reason="Conteo"
    )

    assert ajuste["ingredient"]["stock"] == "4700.000"
    assert ajuste["movement"]["kind"] == "adjustment"
    libro = await client.get(
        f"{INVENTORY_URL}/movements",
        params={"ingredient_id": arroz["id"]},
        headers=authorization_for(local_a.admin),
    )
    cantidades = [movement["quantity"] for movement in libro.json()["items"]]
    assert cantidades == ["-49.500", "-250.500", "5000.000"]
    assert libro.json()["items"][1]["reason"] == "Se mojó el saco"


async def test_el_ajuste_por_conteo_calcula_la_diferencia(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    aceite = await _ingredient(client, local_a, "Aceite", unit="ml")
    await _post(
        client, local_a, "purchases", ingredient_id=aceite["id"], quantity="3000", unit_cost="0.009"
    )

    ajuste = await _post(
        client,
        local_a,
        "adjustments",
        ingredient_id=aceite["id"],
        counted_stock="2800",
        reason="Conteo del viernes",
    )

    assert ajuste["movement"]["quantity"] == "-200.000"
    assert ajuste["ingredient"]["stock"] == "2800.000"


async def test_la_compra_actualiza_el_costo_por_promedio_ponderado(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    arroz = await _ingredient(client, local_a, "Arroz")

    await _post(
        client, local_a, "purchases", ingredient_id=arroz["id"], quantity="5000", unit_cost="0.004"
    )
    segunda = await _post(
        client, local_a, "purchases", ingredient_id=arroz["id"], quantity="20000", unit_cost="0.005"
    )

    assert segunda["ingredient"]["unit_cost"] == "0.004800"
    assert segunda["movement"]["unit_cost"] == "0.005000"


async def test_la_merma_exige_motivo(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    arroz = await _ingredient(client, local_a, "Arroz")

    response = await client.post(
        f"{INVENTORY_URL}/waste",
        json={"ingredient_id": arroz["id"], "quantity": "10", "reason": ""},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 422


async def test_alerta_de_stock_bajo_con_lo_mas_urgente_primero(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    arroz = await _ingredient(client, local_a, "Arroz", min_stock="2000")
    limon = await _ingredient(client, local_a, "Limón", unit="unit", min_stock="30")
    sal = await _ingredient(client, local_a, "Sal", min_stock="500")
    await _post(
        client, local_a, "purchases", ingredient_id=arroz["id"], quantity="1500", unit_cost="0.004"
    )
    await _post(
        client, local_a, "purchases", ingredient_id=sal["id"], quantity="1000", unit_cost="0.0015"
    )
    await _post(
        client, local_a, "adjustments", ingredient_id=limon["id"], quantity="-4", reason="Faltante"
    )

    response = await client.get(
        f"{INVENTORY_URL}/alerts/low-stock", headers=authorization_for(local_a.admin)
    )

    alertas = response.json()
    assert [(a["name"], a["is_negative"]) for a in alertas] == [
        ("Limón", True),
        ("Arroz", False),
    ]
    assert all(a["is_low"] for a in alertas)


async def test_insumo_desactivado_no_alerta_ni_se_compra(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    admin = authorization_for(local_a.admin)
    arroz = await _ingredient(client, local_a, "Arroz", min_stock="2000")
    await client.patch(
        f"{INVENTORY_URL}/ingredients/{arroz['id']}", json={"is_active": False}, headers=admin
    )

    alertas = await client.get(f"{INVENTORY_URL}/alerts/low-stock", headers=admin)
    compra = await client.post(
        f"{INVENTORY_URL}/purchases",
        json={"ingredient_id": arroz["id"], "quantity": "10", "unit_cost": "0.004"},
        headers=admin,
    )

    assert alertas.json() == []
    assert compra.status_code == 409


async def test_receta_costo_y_margen(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    menu = await carta(session, local_a.id)
    admin = authorization_for(local_a.admin)
    carne = await _ingredient(client, local_a, "Lomo de res", unit_cost="0.045")
    papa = await _ingredient(client, local_a, "Papa amarilla", unit_cost="0.0035")

    guardada = await client.put(
        f"{INVENTORY_URL}/recipes/{menu.lomo}",
        json={
            "lines": [
                {"ingredient_id": carne["id"], "quantity": "180"},
                {"ingredient_id": papa["id"], "quantity": "150"},
            ]
        },
        headers=admin,
    )
    leida = await client.get(f"{INVENTORY_URL}/recipes/{menu.lomo}", headers=admin)
    todas = await client.get(f"{INVENTORY_URL}/recipes", headers=admin)

    assert guardada.status_code == 200
    receta = leida.json()
    # 180 g x 0.045 + 150 g x 0.0035 = 8.10 + 0.525 = 8.625 → 8.63
    assert receta["cost"] == "8.63"
    # El margen sale del costo ya redondeado: 28.00 - 8.63, y no 28.00 - 8.625.
    assert receta["margin"] == "19.37"
    assert receta["margin_percent"] == "69.2"
    assert [line["cost"] for line in receta["lines"]] == ["8.1000", "0.5250"]
    por_plato = {row["menu_item_name"]: row for row in todas.json()}
    assert por_plato["Lomo saltado"]["has_recipe"] is True
    assert por_plato["Ají de gallina"]["cost"] is None
    assert por_plato["Ají de gallina"]["margin"] is None


async def test_la_receta_se_reemplaza_entera_y_vacia_se_borra(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    menu = await carta(session, local_a.id)
    admin = authorization_for(local_a.admin)
    carne = await _ingredient(client, local_a, "Lomo de res", unit_cost="0.045")
    url = f"{INVENTORY_URL}/recipes/{menu.lomo}"

    repetida = await client.put(
        url,
        json={
            "lines": [
                {"ingredient_id": carne["id"], "quantity": "100"},
                {"ingredient_id": carne["id"], "quantity": "80"},
            ]
        },
        headers=admin,
    )
    await client.put(
        url, json={"lines": [{"ingredient_id": carne["id"], "quantity": "180"}]}, headers=admin
    )
    vacia = await client.put(url, json={"lines": []}, headers=admin)

    assert repetida.status_code == 422
    assert vacia.json()["lines"] == []
    assert vacia.json()["has_recipe"] is False


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/ingredients", None),
        ("GET", "/alerts/low-stock", None),
        ("GET", "/movements", None),
        ("GET", "/recipes", None),
        ("POST", "/ingredients", {"name": "Sal", "unit": "g"}),
        ("POST", "/purchases", {"ingredient_id": 1, "quantity": "1", "unit_cost": "1"}),
        ("POST", "/waste", {"ingredient_id": 1, "quantity": "1", "reason": "x"}),
        ("POST", "/adjustments", {"ingredient_id": 1, "quantity": "1", "reason": "x"}),
        ("PUT", "/recipes/1", {"lines": []}),
    ],
)
async def test_el_mesero_no_entra_al_inventario(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    response = await client.request(
        method, INVENTORY_URL + path, json=body, headers=authorization_for(local_a.waiter)
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/ingredients/{id}", None),
        ("PATCH", "/ingredients/{id}", {"name": "Intruso"}),
        ("POST", "/purchases", {"ingredient_id": "{id}", "quantity": "1", "unit_cost": "1"}),
        ("POST", "/waste", {"ingredient_id": "{id}", "quantity": "1", "reason": "x"}),
        ("POST", "/adjustments", {"ingredient_id": "{id}", "quantity": "1", "reason": "x"}),
    ],
)
async def test_un_local_no_ve_ni_toca_los_insumos_de_otro(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    ajeno = await _ingredient(client, local_b, "Arroz")
    if body is not None:
        body = {key: ajeno["id"] if value == "{id}" else value for key, value in body.items()}

    response = await client.request(
        method,
        INVENTORY_URL + path.format(id=ajeno["id"]),
        json=body,
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 404
    propio = await client.get(
        f"{INVENTORY_URL}/ingredients/{ajeno['id']}", headers=authorization_for(local_b.admin)
    )
    assert propio.json()["name"] == "Arroz"
    assert propio.json()["stock"] == "0.000"
    listado_a = await client.get(
        f"{INVENTORY_URL}/ingredients", headers=authorization_for(local_a.admin)
    )
    assert listado_a.json() == []


async def test_las_recetas_no_cruzan_de_local(
    client: AsyncClient,
    session: AsyncSession,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
) -> None:
    menu_a: Carta = await carta(session, local_a.id)
    menu_b: Carta = await carta(session, local_b.id)
    admin_a = authorization_for(local_a.admin)
    propio = await _ingredient(client, local_a, "Carne")
    ajeno = await _ingredient(client, local_b, "Carne")

    plato_ajeno = await client.put(
        f"{INVENTORY_URL}/recipes/{menu_b.lomo}",
        json={"lines": [{"ingredient_id": propio["id"], "quantity": "1"}]},
        headers=admin_a,
    )
    insumo_ajeno = await client.put(
        f"{INVENTORY_URL}/recipes/{menu_a.lomo}",
        json={"lines": [{"ingredient_id": ajeno["id"], "quantity": "1"}]},
        headers=admin_a,
    )
    leer_ajeno = await client.get(f"{INVENTORY_URL}/recipes/{menu_b.lomo}", headers=admin_a)

    assert plato_ajeno.status_code == 404
    assert insumo_ajeno.status_code == 404
    assert leer_ajeno.status_code == 404
