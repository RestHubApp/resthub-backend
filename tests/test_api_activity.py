"""La bitácora por HTTP: quién la ve y de qué restaurante."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import VALID_PASSWORD, StaffedRestaurant, authorization_for

URL = "/api/v1/activity"
LOGIN_URL = "/api/v1/auth/login"


async def _entrar(client: AsyncClient, email: str) -> None:
    response = await client.post(LOGIN_URL, json={"email": email, "password": VALID_PASSWORD})
    assert response.status_code == 200


async def test_el_encargado_ve_solo_los_movimientos_de_su_restaurante(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    await _entrar(client, "mesero@local-a.pe")
    await _entrar(client, "mesero@local-b.pe")
    await _entrar(client, "encargado@local-b.pe")

    response = await client.get(URL, headers=authorization_for(local_a.admin))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    entrada = body["items"][0]
    assert entrada["kind"] == "signed_in"
    assert entrada["kind_label"] == "Inició sesión"
    assert (entrada["user_id"], entrada["user_name"]) == (local_a.waiter.id, "Luis Torres")
    assert entrada["user_role_id"] == local_a.waiter.role.id
    assert entrada["user_role_label"] == "Mesero"


async def test_la_bitacora_filtra_por_rol(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    await _entrar(client, "mesero@local-a.pe")
    await _entrar(client, "encargado@local-a.pe")

    response = await client.get(
        URL, params={"role_id": local_a.admin.role.id}, headers=authorization_for(local_a.admin)
    )

    assert [item["user_id"] for item in response.json()["items"]] == [local_a.admin.id]


async def test_el_mesero_no_ve_la_bitacora(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    response = await client.get(URL, headers=authorization_for(local_a.waiter))

    assert response.status_code == 403
