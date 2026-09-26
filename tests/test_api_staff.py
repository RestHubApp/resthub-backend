"""Gestión del personal por HTTP: permisos y aislamiento entre restaurantes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import VALID_PASSWORD, StaffedRestaurant, authorization_for

STAFF_URL = "/api/v1/staff"
LOGIN_URL = "/api/v1/auth/login"


def _nuevo_mesero(local: StaffedRestaurant) -> dict[str, object]:
    return {
        "email": "carla@local-a.pe",
        "full_name": "Carla Ríos",
        "role_id": local.waiter.role.id,
        "password": VALID_PASSWORD,
    }


async def test_el_encargado_lista_solo_a_su_personal(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    response = await client.get(STAFF_URL, headers=authorization_for(local_a.admin))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["email"] for item in body["items"]} == {
        "encargado@local-a.pe",
        "mesero@local-a.pe",
    }
    assert {item["role_label"] for item in body["items"]} == {"Encargado", "Mesero"}


async def test_el_listado_filtra_por_rol_y_busca(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    headers = authorization_for(local_a.admin)

    meseros = await client.get(
        STAFF_URL, params={"role_id": local_a.waiter.role.id}, headers=headers
    )
    busqueda = await client.get(STAFF_URL, params={"search": "rosa"}, headers=headers)

    assert [item["id"] for item in meseros.json()["items"]] == [local_a.waiter.id]
    assert [item["id"] for item in busqueda.json()["items"]] == [local_a.admin.id]


async def test_el_alta_queda_en_el_restaurante_del_encargado(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    response = await client.post(
        STAFF_URL, json=_nuevo_mesero(local_a), headers=authorization_for(local_a.admin)
    )

    assert response.status_code == 201
    assert response.json()["role_id"] == local_a.waiter.role.id
    assert response.json()["role_label"] == "Mesero"
    assert "role" not in response.json()
    login = await client.post(
        LOGIN_URL, json={"email": "carla@local-a.pe", "password": VALID_PASSWORD}
    )
    assert login.json()["restaurant"]["id"] == local_a.id


async def test_un_restaurant_id_en_el_cuerpo_se_ignora(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    """Nunca se acepta un restaurante enviado por el cliente."""
    response = await client.post(
        STAFF_URL,
        json={**_nuevo_mesero(local_a), "restaurant_id": local_b.id},
        headers=authorization_for(local_a.admin),
    )
    listado_b = await client.get(STAFF_URL, headers=authorization_for(local_b.admin))

    assert response.status_code == 201
    assert "carla@local-a.pe" not in {item["email"] for item in listado_b.json()["items"]}


async def test_un_correo_de_otro_restaurante_choca(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    response = await client.post(
        STAFF_URL,
        json={**_nuevo_mesero(local_a), "email": "mesero@local-b.pe"},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 409


async def test_editar_nombre_y_rol(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    response = await client.patch(
        f"{STAFF_URL}/{local_a.waiter.id}",
        json={"full_name": "Luis A. Torres", "role_id": local_a.admin.role.id},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Luis A. Torres"
    assert response.json()["role_id"] == local_a.admin.role.id
    assert response.json()["role_label"] == "Encargado"


async def test_el_encargado_no_cambia_su_propio_rol(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.patch(
        f"{STAFF_URL}/{local_a.admin.id}",
        json={"role_id": local_a.waiter.role.id},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 409


async def test_desactivar_corta_el_acceso(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    mesero = authorization_for(local_a.waiter)

    response = await client.patch(
        f"{STAFF_URL}/{local_a.waiter.id}/status",
        json={"is_active": False},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert (await client.get("/api/v1/auth/me", headers=mesero)).status_code == 401


async def test_el_encargado_no_se_desactiva_a_si_mismo(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.patch(
        f"{STAFF_URL}/{local_a.admin.id}/status",
        json={"is_active": False},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 409


async def test_restablecer_la_contrasena_de_un_mesero(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    nueva = "nueva-contrasena-larga"
    response = await client.post(
        f"{STAFF_URL}/{local_a.waiter.id}/password",
        json={"new_password": nueva},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 204
    login = await client.post(LOGIN_URL, json={"email": "mesero@local-a.pe", "password": nueva})
    assert login.status_code == 200


async def test_la_contrasena_propia_no_se_restablece_por_esta_via(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.post(
        f"{STAFF_URL}/{local_a.admin.id}/password",
        json={"new_password": "nueva-contrasena-larga"},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"full_name": "Intruso"}),
        ("PATCH", "/status", {"is_active": False}),
        ("POST", "/password", {"new_password": "contrasena-intrusa"}),
    ],
)
async def test_un_encargado_no_ve_ni_toca_personal_de_otro_restaurante(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    response = await client.request(
        method,
        f"{STAFF_URL}/{local_b.waiter.id}{suffix}",
        json=body,
        headers=authorization_for(local_a.admin),
    )

    # 404 y no 403: responder "existe pero no es tuya" delataría a otros locales.
    assert response.status_code == 404
    intacto = await client.post(
        LOGIN_URL, json={"email": "mesero@local-b.pe", "password": VALID_PASSWORD}
    )
    assert intacto.status_code == 200
    assert intacto.json()["user"]["full_name"] == "Luis Torres"


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        (
            "POST",
            "",
            {"email": "x@local-a.pe", "full_name": "X", "role_id": 1, "password": "p" * 12},
        ),
        ("GET", "/{id}", None),
        ("PATCH", "/{id}", {"role_id": 1}),
        ("PATCH", "/{id}/status", {"is_active": False}),
        ("POST", "/{id}/password", {"new_password": "contrasena-intrusa"}),
    ],
)
async def test_el_mesero_no_gestiona_personal(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    url = STAFF_URL + suffix.replace("{id}", str(local_a.admin.id))

    response = await client.request(
        method, url, json=body, headers=authorization_for(local_a.waiter)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Tu rol no tiene permiso para esta acción."
