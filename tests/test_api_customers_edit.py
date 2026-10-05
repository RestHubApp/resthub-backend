"""Edición y búsqueda de la libreta de clientes por HTTP."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import StaffedRestaurant, authorization_for

CUSTOMERS_URL = "/api/v1/customers"


async def _alta(client: AsyncClient, local: StaffedRestaurant, **datos: str) -> int:
    response = await client.post(
        CUSTOMERS_URL,
        json={"name": "Ana Torres", "consent": True, **datos},
        headers=authorization_for(local.admin),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_el_encargado_edita_la_ficha_y_la_busqueda_la_refleja(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    ana = await _alta(client, local_a, phone="987 654 321")
    await _alta(client, local_a, name="Beto Ruiz")

    editado = await client.put(
        f"{CUSTOMERS_URL}/{ana}",
        json={"name": "Ana Torres Vega", "phone": "987654321", "notes": "Sin cebolla"},
        headers=authorization_for(local_a.admin),
    )
    todos = await client.get(CUSTOMERS_URL, headers=authorization_for(local_a.waiter))

    assert editado.status_code == 200, editado.text
    assert editado.json()["name"] == "Ana Torres Vega"
    assert editado.json()["notes"] == "Sin cebolla"
    assert todos.json()["total"] == 2
    assert [c["name"] for c in todos.json()["items"]] == ["Ana Torres Vega", "Beto Ruiz"]


async def test_editar_con_datos_invalidos_o_telefono_ajeno_no_guarda(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    await _alta(client, local_a, phone="987 654 321")
    beto = await _alta(client, local_a, name="Beto Ruiz")
    admin = authorization_for(local_a.admin)

    ajeno = await client.put(
        f"{CUSTOMERS_URL}/{beto}", json={"name": "Beto", "phone": "987654321"}, headers=admin
    )
    invalido = await client.put(
        f"{CUSTOMERS_URL}/{beto}", json={"name": "Beto", "email": "sin-arroba"}, headers=admin
    )
    ficha = await client.get(f"{CUSTOMERS_URL}/{beto}", headers=admin)

    assert ajeno.status_code == 409
    assert invalido.status_code == 422
    assert invalido.json()["detail"] == "El correo no es válido."
    assert ficha.json()["name"] == "Beto Ruiz"
    assert ficha.json()["phone"] == ""


async def test_un_local_no_edita_los_clientes_de_otro(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    ana = await _alta(client, local_a)

    response = await client.put(
        f"{CUSTOMERS_URL}/{ana}", json={"name": "Robado"}, headers=authorization_for(local_b.admin)
    )
    ficha = await client.get(f"{CUSTOMERS_URL}/{ana}", headers=authorization_for(local_a.admin))

    assert response.status_code == 404
    assert ficha.json()["name"] == "Ana Torres"


async def test_sin_consentimiento_no_se_guarda_y_con_el_queda_en_la_ficha(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    admin = authorization_for(local_a.admin)
    sin = await client.post(CUSTOMERS_URL, json={"name": "Ana"}, headers=admin)
    con = await client.post(CUSTOMERS_URL, json={"name": "Ana", "consent": True}, headers=admin)
    todos = await client.get(CUSTOMERS_URL, headers=admin)

    assert sin.status_code == 422
    assert "datos personales" in sin.json()["detail"]
    assert con.status_code == 201
    assert con.json()["consent_at"] is not None
    assert con.json()["consent_version"]
    assert todos.json()["total"] == 1
