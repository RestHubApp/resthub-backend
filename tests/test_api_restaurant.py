"""El restaurante propio por HTTP."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import StaffedRestaurant, authorization_for

URL = "/api/v1/restaurant"


async def test_cada_cuenta_lee_su_propio_restaurante(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    del local_a
    response = await client.get(URL, headers=authorization_for(local_b.waiter))

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["slug"], body["timezone"]) == (local_b.id, "local-b", "America/Lima")


async def test_el_encargado_edita_nombre_y_zona(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    response = await client.patch(
        URL,
        json={"name": "  Cevichería   Doña Rosa ", "timezone": "America/Bogota"},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Cevichería Doña Rosa"
    assert response.json()["timezone"] == "America/Bogota"
    # El slug no cambia, y el otro restaurante tampoco.
    assert response.json()["slug"] == "local-a"
    otro = await client.get(URL, headers=authorization_for(local_b.admin))
    assert otro.json()["name"] == "Restaurante local-b"


async def test_el_nombre_nuevo_se_ve_en_la_sesion(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    await client.patch(URL, json={"name": "Doña Rosa"}, headers=authorization_for(local_a.admin))

    me = await client.get("/api/v1/auth/me", headers=authorization_for(local_a.waiter))

    assert me.json()["restaurant"]["name"] == "Doña Rosa"


async def test_una_zona_inexistente_se_rechaza(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.patch(
        URL, json={"timezone": "Hora de Lima"}, headers=authorization_for(local_a.admin)
    )

    assert response.status_code == 422


async def test_el_mesero_no_edita_el_restaurante(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.patch(
        URL, json={"name": "Intruso"}, headers=authorization_for(local_a.waiter)
    )

    assert response.status_code == 403


async def test_sin_credencial_no_hay_restaurante(client: AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401
