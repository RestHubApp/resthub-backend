"""El menú por HTTP: permisos, lo que ve cada rol y aislamiento entre locales."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import StaffedRestaurant, authorization_for

MENU_URL = "/api/v1/menu"


async def _category(client: AsyncClient, local: StaffedRestaurant, name: str) -> int:
    response = await client.post(
        f"{MENU_URL}/categories", json={"name": name}, headers=authorization_for(local.admin)
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _dish(
    client: AsyncClient, local: StaffedRestaurant, category_id: int, name: str, price: str
) -> int:
    response = await client.post(
        f"{MENU_URL}/items",
        json={"category_id": category_id, "name": name, "price": price},
        headers=authorization_for(local.admin),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_el_menu_se_agrupa_por_categoria_en_orden(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    fondos = await _category(client, local_a, "Fondos")
    bebidas = await _category(client, local_a, "Bebidas")
    await _dish(client, local_a, fondos, "Lomo saltado", "28.00")
    await _dish(client, local_a, fondos, "Ají de gallina", "22.00")
    await _dish(client, local_a, bebidas, "Chicha morada", "5.00")

    response = await client.get(MENU_URL, headers=authorization_for(local_a.waiter))

    assert response.status_code == 200
    categorias = response.json()["categories"]
    assert [c["name"] for c in categorias] == ["Fondos", "Bebidas"]
    assert [i["name"] for i in categorias[0]["items"]] == ["Lomo saltado", "Ají de gallina"]
    assert categorias[0]["items"][0]["price"] == "28.00"


async def test_el_mesero_no_ve_lo_retirado_pero_si_lo_agotado(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    admin = authorization_for(local_a.admin)
    fondos = await _category(client, local_a, "Fondos")
    retirada = await _category(client, local_a, "Temporada")
    lomo = await _dish(client, local_a, fondos, "Lomo saltado", "28.00")
    seco = await _dish(client, local_a, fondos, "Seco de res", "25.00")
    await client.patch(f"{MENU_URL}/items/{seco}", json={"is_active": False}, headers=admin)
    await client.patch(
        f"{MENU_URL}/categories/{retirada}", json={"is_active": False}, headers=admin
    )
    agotado = await client.patch(
        f"{MENU_URL}/items/{lomo}/availability", json={"is_available": False}, headers=admin
    )
    assert agotado.json()["is_available"] is False

    mesero = await client.get(
        MENU_URL, params={"include_inactive": True}, headers=authorization_for(local_a.waiter)
    )
    encargado = await client.get(MENU_URL, params={"include_inactive": True}, headers=admin)

    assert [c["name"] for c in mesero.json()["categories"]] == ["Fondos"]
    platos = mesero.json()["categories"][0]["items"]
    assert [(p["name"], p["is_available"]) for p in platos] == [("Lomo saltado", False)]
    assert len(encargado.json()["categories"]) == 2
    assert len(encargado.json()["categories"][0]["items"]) == 2
    detalle = await client.get(
        f"{MENU_URL}/items/{seco}", headers=authorization_for(local_a.waiter)
    )
    assert detalle.status_code == 404


async def test_editar_precio_y_mover_de_categoria(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    fondos = await _category(client, local_a, "Fondos")
    marinos = await _category(client, local_a, "Marinos")
    ceviche = await _dish(client, local_a, fondos, "Ceviche", "30.00")

    response = await client.patch(
        f"{MENU_URL}/items/{ceviche}",
        json={"category_id": marinos, "price": "32.50", "description": "Pesca del día"},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 200
    assert response.json()["category_id"] == marinos
    assert response.json()["price"] == "32.50"


async def test_nombres_repetidos_chocan(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    admin = authorization_for(local_a.admin)
    fondos = await _category(client, local_a, "Fondos")
    await _dish(client, local_a, fondos, "Ceviche", "30.00")

    categoria = await client.post(f"{MENU_URL}/categories", json={"name": "FONDOS"}, headers=admin)
    plato = await client.post(
        f"{MENU_URL}/items",
        json={"category_id": fondos, "name": "ceviche", "price": "30"},
        headers=admin,
    )

    assert categoria.status_code == 409
    assert plato.status_code == 409


async def test_un_precio_con_tres_decimales_se_rechaza(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    fondos = await _category(client, local_a, "Fondos")

    response = await client.post(
        f"{MENU_URL}/items",
        json={"category_id": fondos, "name": "Ceviche", "price": "30.005"},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 422


async def test_reordenar_categorias_y_platos(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    admin = authorization_for(local_a.admin)
    entradas = await _category(client, local_a, "Entradas")
    fondos = await _category(client, local_a, "Fondos")
    papa = await _dish(client, local_a, entradas, "Papa a la huancaína", "12.00")
    causa = await _dish(client, local_a, entradas, "Causa limeña", "14.00")

    categorias = await client.put(
        f"{MENU_URL}/categories/order", json={"ids": [fondos, entradas]}, headers=admin
    )
    platos = await client.put(
        f"{MENU_URL}/categories/{entradas}/items/order", json={"ids": [causa, papa]}, headers=admin
    )
    incompleto = await client.put(
        f"{MENU_URL}/categories/order", json={"ids": [fondos]}, headers=admin
    )

    assert categorias.status_code == 200
    assert platos.status_code == 200
    assert incompleto.status_code == 422
    menu = (await client.get(MENU_URL, headers=admin)).json()["categories"]
    assert [c["id"] for c in menu] == [fondos, entradas]
    assert [p["id"] for p in menu[1]["items"]] == [causa, papa]


async def test_solo_se_borra_una_categoria_vacia(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    admin = authorization_for(local_a.admin)
    fondos = await _category(client, local_a, "Fondos")
    vacia = await _category(client, local_a, "Postres")
    await _dish(client, local_a, fondos, "Ceviche", "30.00")

    con_platos = await client.delete(f"{MENU_URL}/categories/{fondos}", headers=admin)
    sin_platos = await client.delete(f"{MENU_URL}/categories/{vacia}", headers=admin)

    assert con_platos.status_code == 409
    assert sin_platos.status_code == 204


async def test_los_cambios_quedan_en_la_bitacora(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    fondos = await _category(client, local_a, "Fondos")
    ceviche = await _dish(client, local_a, fondos, "Ceviche", "30.00")
    await client.patch(
        f"{MENU_URL}/items/{ceviche}/availability",
        json={"is_available": False},
        headers=authorization_for(local_a.admin),
    )

    response = await client.get("/api/v1/activity", headers=authorization_for(local_a.admin))

    kinds = [entry["kind"] for entry in response.json()["items"]]
    assert kinds[:3] == ["menu_item_availability", "menu_item_created", "menu_category_created"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/categories", {"name": "Postres"}),
        ("PATCH", "/categories/{category}", {"name": "Otra"}),
        ("DELETE", "/categories/{category}", None),
        ("PUT", "/categories/order", {"ids": []}),
        ("POST", "/items", {"category_id": 1, "name": "X", "price": "1"}),
        ("PATCH", "/items/{item}", {"price": "1.00"}),
        ("PATCH", "/items/{item}/availability", {"is_available": False}),
    ],
)
async def test_el_mesero_no_edita_el_menu(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    categoria = await _category(client, local_a, "Fondos")
    plato = await _dish(client, local_a, categoria, "Ceviche", "30.00")
    url = MENU_URL + path.format(category=categoria, item=plato)

    response = await client.request(
        method, url, json=body, headers=authorization_for(local_a.waiter)
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/items/{item}", None),
        ("PATCH", "/items/{item}", {"price": "1.00"}),
        ("PATCH", "/items/{item}/availability", {"is_available": False}),
        ("PATCH", "/categories/{category}", {"name": "Intrusa"}),
        ("DELETE", "/categories/{category}", None),
        ("PUT", "/categories/{category}/items/order", {"ids": []}),
    ],
)
async def test_un_local_no_ve_ni_toca_el_menu_de_otro(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    categoria = await _category(client, local_b, "Fondos")
    plato = await _dish(client, local_b, categoria, "Ceviche", "30.00")
    url = MENU_URL + path.format(category=categoria, item=plato)

    response = await client.request(
        method, url, json=body, headers=authorization_for(local_a.admin)
    )

    assert response.status_code == 404
    menu_a = await client.get(MENU_URL, headers=authorization_for(local_a.admin))
    assert menu_a.json()["categories"] == []
    menu_b = await client.get(MENU_URL, headers=authorization_for(local_b.admin))
    assert menu_b.json()["categories"][0]["items"][0]["price"] == "30.00"


async def test_un_plato_no_se_crea_en_la_categoria_de_otro_local(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    ajena = await _category(client, local_b, "Fondos")

    response = await client.post(
        f"{MENU_URL}/items",
        json={"category_id": ajena, "name": "Intruso", "price": "1.00"},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 404
