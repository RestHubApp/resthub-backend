"""Opciones de los platos y platos agotados por falta de insumos, por HTTP."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import Carta, carta
from tests.conftest import StaffedRestaurant, authorization_for

MENU_URL = "/api/v1/menu"
ORDERS_URL = "/api/v1/orders"

TAMANIO = {
    "name": "Tamaño",
    "min_choices": 1,
    "max_choices": 1,
    "options": [{"name": "Personal", "price": "0"}, {"name": "Familiar", "price": "10.50"}],
}
EXTRAS = {
    "name": "Extras",
    "min_choices": 0,
    "max_choices": 2,
    "options": [
        {"name": "Huevo frito", "price": "2"},
        {"name": "Plátano", "price": "2.50"},
        {"name": "Sin cebolla", "price": "0"},
    ],
}


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    return await carta(session, local_a.id)


async def _con_opciones(client: AsyncClient, local: StaffedRestaurant, dish: int) -> dict[str, Any]:
    response = await client.patch(
        f"{MENU_URL}/items/{dish}",
        json={"modifier_groups": [TAMANIO, EXTRAS]},
        headers=authorization_for(local.admin),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _pedir(
    client: AsyncClient, local: StaffedRestaurant, dish: int, modifiers: list[dict[str, str]]
) -> Any:
    return await client.post(
        ORDERS_URL,
        json={
            "type": "takeaway",
            "items": [{"menu_item_id": dish, "quantity": 2, "modifiers": modifiers}],
        },
        headers=authorization_for(local.waiter),
    )


async def test_el_plato_guarda_sus_grupos_de_opciones(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    plato = await _con_opciones(client, local_a, carta_a.lomo)
    carta_del_mesero = await client.get(MENU_URL, headers=authorization_for(local_a.waiter))

    assert [g["name"] for g in plato["modifier_groups"]] == ["Tamaño", "Extras"]
    assert plato["modifier_groups"][0]["options"][1] == {"name": "Familiar", "price": "10.50"}
    lomo = next(
        item
        for section in carta_del_mesero.json()["categories"]
        for item in section["items"]
        if item["id"] == carta_a.lomo
    )
    assert lomo["modifier_groups"][1]["max_choices"] == 2


async def test_grupos_mal_armados_se_rechazan(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    malo = {**EXTRAS, "min_choices": 3, "max_choices": 2}

    response = await client.patch(
        f"{MENU_URL}/items/{carta_a.lomo}",
        json={"modifier_groups": [malo]},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 422


async def test_las_opciones_elegidas_suman_al_precio_y_quedan_congeladas(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _con_opciones(client, local_a, carta_a.lomo)

    response = await _pedir(
        client,
        local_a,
        carta_a.lomo,
        [
            {"group": "Tamaño", "option": "Familiar"},
            {"group": "Extras", "option": "Plátano"},
            {"group": "extras", "option": "sin cebolla"},
        ],
    )

    item = response.json()["items"][0]
    # 28.00 + 10.50 + 2.50 = 41.00 por plato; dos platos.
    assert item["unit_price"] == "41.00"
    assert response.json()["total"] == "82.00"
    assert [(m["option"], m["price"]) for m in item["modifiers"]] == [
        ("Familiar", "10.50"),
        ("Plátano", "2.50"),
        ("Sin cebolla", "0.00"),
    ]


@pytest.mark.parametrize(
    "modifiers",
    [
        [],
        [{"group": "Tamaño", "option": "Mediano"}],
        [{"group": "Término", "option": "Tres cuartos"}],
        [
            {"group": "Tamaño", "option": "Personal"},
            {"group": "Extras", "option": "Huevo frito"},
            {"group": "Extras", "option": "Plátano"},
            {"group": "Extras", "option": "Sin cebolla"},
        ],
    ],
    ids=["falta-el-obligatorio", "opcion-inexistente", "grupo-inexistente", "pasa-el-maximo"],
)
async def test_una_eleccion_invalida_no_se_pide(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    carta_a: Carta,
    modifiers: list[dict[str, str]],
) -> None:
    await _con_opciones(client, local_a, carta_a.lomo)

    response = await _pedir(client, local_a, carta_a.lomo, modifiers)

    assert response.status_code == 422


async def test_el_plato_sin_insumos_se_agota_solo(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    encargado = authorization_for(local_a.admin)
    insumo = await client.post(
        "/api/v1/inventory/ingredients",
        json={"name": "Carne de res", "unit": "g", "min_stock": "0", "unit_cost": "0.04"},
        headers=encargado,
    )
    ingredient_id = insumo.json()["id"]
    await client.put(
        f"/api/v1/inventory/recipes/{carta_a.lomo}",
        json={"lines": [{"ingredient_id": ingredient_id, "quantity": "180"}]},
        headers=encargado,
    )

    sin_stock = await _pedir(client, local_a, carta_a.lomo, [])
    carta_vacia = await client.get(MENU_URL, headers=authorization_for(local_a.waiter))
    await client.post(
        "/api/v1/inventory/purchases",
        json={"ingredient_id": ingredient_id, "quantity": "500", "unit_cost": "0.04"},
        headers=encargado,
    )
    con_stock = await _pedir(client, local_a, carta_a.lomo, [])

    def agotado(menu: Any) -> bool:
        return next(
            item["out_of_stock"]
            for section in menu["categories"]
            for item in section["items"]
            if item["id"] == carta_a.lomo
        )

    assert sin_stock.status_code == 409
    assert agotado(carta_vacia.json()) is True
    assert con_stock.status_code == 201
    # Con la compra, el plato vuelve a la carta sin que nadie lo toque.
    assert agotado((await client.get(MENU_URL, headers=encargado)).json()) is False
