"""Roles por restaurante por HTTP: quién los arma, qué se puede cambiar y el aislamiento."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import CATALOG, Permission
from resthub.core.realtime import PERMISSIONS_TOPIC
from resthub.core.realtime_broker import LocalBroker
from tests.builders import Carta, carta
from tests.conftest import VALID_PASSWORD, StaffedRestaurant, authorization_for

ROLES_URL = "/api/v1/roles"
PERMISSIONS_URL = "/api/v1/permissions"
STAFF_URL = "/api/v1/staff"
LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/auth/me"
ORDERS_URL = "/api/v1/orders"
COCINA = ["menu.read", "orders.manage", "orders.read_all"]


async def _crear_rol(
    client: AsyncClient, headers: dict[str, str], name: str, permissions: list[str]
) -> dict[str, Any]:
    response = await client.post(
        ROLES_URL, json={"name": name, "permissions": permissions}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _cuenta_con_rol(
    client: AsyncClient, local: StaffedRestaurant, role_id: int, email: str
) -> dict[str, str]:
    """Da de alta una cuenta con ese rol y devuelve su cabecera de acceso."""
    alta = await client.post(
        STAFF_URL,
        json={
            "email": email,
            "full_name": "Cuenta de prueba",
            "role_id": role_id,
            "password": VALID_PASSWORD,
        },
        headers=authorization_for(local.admin),
    )
    assert alta.status_code == 201, alta.text
    login = await client.post(LOGIN_URL, json={"email": email, "password": VALID_PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_el_catalogo_viene_por_grupo_con_etiquetas(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.get(PERMISSIONS_URL, headers=authorization_for(local_a.admin))

    assert response.status_code == 200
    body = response.json()
    assert {item["code"] for item in body} == {permission.value for permission in Permission}
    assert body[0] == {"code": "menu.read", "label": "Ver el menú", "group": "Menú"}
    assert body[-1] == {
        "code": "roles.manage",
        "label": "Crear roles y elegir sus permisos",
        "group": "Administración",
    }
    grupos = [item["group"] for item in body]
    assert grupos == sorted(grupos, key=grupos.index)
    assert len(body) == len(CATALOG)


async def test_el_restaurante_nace_con_encargado_y_mesero(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.get(ROLES_URL, headers=authorization_for(local_a.admin))

    assert response.status_code == 200
    encargado, mesero = response.json()
    assert encargado == {
        "id": local_a.admin.role.id,
        "name": "Encargado",
        "kind": "owner",
        "permissions": encargado["permissions"],
        "member_count": 1,
        "is_editable": False,
        "is_deletable": False,
    }
    assert set(encargado["permissions"]) == {permission.value for permission in Permission}
    assert (mesero["name"], mesero["kind"], mesero["member_count"]) == ("Mesero", "waiter", 1)
    assert (mesero["is_editable"], mesero["is_deletable"]) == (True, False)
    assert "orders.charge" in mesero["permissions"]


async def test_los_roles_propios_van_despues_y_por_nombre(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    headers = authorization_for(local_a.admin)
    await _crear_rol(client, headers, "Cocinero", COCINA)
    await _crear_rol(client, headers, "Cajero", ["orders.charge"])

    response = await client.get(ROLES_URL, headers=headers)

    assert [role["name"] for role in response.json()] == [
        "Encargado",
        "Mesero",
        "Cajero",
        "Cocinero",
    ]


async def test_crear_un_rol_queda_en_la_bitacora(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    headers = authorization_for(local_a.admin)

    creado = await _crear_rol(client, headers, "  Cocinero  ", COCINA)
    bitacora = await client.get(
        "/api/v1/activity", params={"kind": "role_created"}, headers=headers
    )

    assert creado["name"] == "Cocinero"
    assert creado["kind"] == "custom"
    assert creado["permissions"] == ["menu.read", "orders.read_all", "orders.manage"]
    assert (creado["member_count"], creado["is_editable"], creado["is_deletable"]) == (
        0,
        True,
        True,
    )
    assert [item["detail"] for item in bitacora.json()["items"]] == ["Cocinero (3 permisos)"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"name": "Cocinero", "permissions": ["cocina.volar"]}, 422),
        ({"name": "   ", "permissions": []}, 422),
        ({"name": "x" * 41, "permissions": []}, 422),
        ({"name": "mesero", "permissions": []}, 409),
        ({"name": "ENCARGADO", "permissions": []}, 409),
    ],
)
async def test_validaciones_al_crear(
    client: AsyncClient, local_a: StaffedRestaurant, body: dict[str, object], status: int
) -> None:
    response = await client.post(ROLES_URL, json=body, headers=authorization_for(local_a.admin))

    assert response.status_code == status, response.text


async def test_el_nombre_se_repite_entre_restaurantes_pero_no_dentro_de_uno(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    await _crear_rol(client, authorization_for(local_a.admin), "Cocinero", COCINA)

    otro_local = await client.post(
        ROLES_URL,
        json={"name": "Cocinero", "permissions": COCINA},
        headers=authorization_for(local_b.admin),
    )
    repetido = await client.post(
        ROLES_URL,
        json={"name": "cocinero", "permissions": []},
        headers=authorization_for(local_a.admin),
    )

    assert otro_local.status_code == 201
    assert repetido.status_code == 409


async def test_nadie_da_permisos_que_no_tiene(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    encargado = authorization_for(local_a.admin)
    arma_roles = await _crear_rol(client, encargado, "Arma roles", ["roles.manage", "menu.read"])
    headers = await _cuenta_con_rol(client, local_a, arma_roles["id"], "roles@local-a.pe")
    cajero = await _crear_rol(client, encargado, "Cajero", ["orders.charge"])

    crear = await client.post(
        ROLES_URL, json={"name": "Jefe", "permissions": ["staff.manage"]}, headers=headers
    )
    ascenderse = await client.put(
        f"{ROLES_URL}/{arma_roles['id']}",
        json={"name": "Arma roles", "permissions": ["roles.manage", "menu.read", "cash.manage"]},
        headers=headers,
    )
    recortar_ajeno = await client.put(
        f"{ROLES_URL}/{cajero['id']}",
        json={"name": "Cajero", "permissions": []},
        headers=headers,
    )
    permitido = await client.post(
        ROLES_URL, json={"name": "Solo menú", "permissions": ["menu.read"]}, headers=headers
    )

    assert crear.status_code == 403
    assert crear.json()["detail"] == "No puedes dar permisos que no tienes."
    assert ascenderse.status_code == 403
    assert recortar_ajeno.status_code == 403
    assert permitido.status_code == 201


async def test_el_encargado_no_se_edita_ni_se_borra(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    headers = authorization_for(local_a.admin)
    url = f"{ROLES_URL}/{local_a.admin.role.id}"

    editar = await client.put(url, json={"name": "Encargado", "permissions": []}, headers=headers)
    borrar = await client.delete(url, headers=headers)
    me = await client.get(ME_URL, headers=headers)

    assert editar.status_code == 409
    assert borrar.status_code == 409
    assert len(me.json()["permissions"]) == len(Permission)


async def test_el_mesero_cambia_de_permisos_pero_no_de_nombre(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    headers = authorization_for(local_a.admin)
    url = f"{ROLES_URL}/{local_a.waiter.role.id}"

    renombrar = await client.put(
        url, json={"name": "Camarero", "permissions": ["menu.read"]}, headers=headers
    )
    recortar = await client.put(
        url, json={"name": "mesero", "permissions": ["menu.read", "orders.take"]}, headers=headers
    )
    borrar = await client.delete(url, headers=headers)
    me = await client.get(ME_URL, headers=authorization_for(local_a.waiter))

    assert renombrar.status_code == 409
    assert recortar.status_code == 200
    assert recortar.json()["name"] == "Mesero"
    assert borrar.status_code == 409
    assert me.json()["permissions"] == ["menu.read", "orders.take"]
    assert me.json()["user"]["role_label"] == "Mesero"


async def test_cambiar_los_permisos_avisa_a_quienes_tienen_el_rol(
    client: AsyncClient, local_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    async with broker.subscribe() as queue:
        response = await client.put(
            f"{ROLES_URL}/{local_a.waiter.role.id}",
            json={"name": "Mesero", "permissions": ["menu.read"]},
            headers=authorization_for(local_a.admin),
        )
        await asyncio.sleep(0)

        assert response.status_code == 200
        received = queue.get_nowait()
        assert queue.empty()
    assert received.topic == PERMISSIONS_TOPIC
    assert received.restaurant_id == local_a.id
    assert received.user_ids == {local_a.waiter.id}


async def test_guardar_sin_cambios_no_avisa(
    client: AsyncClient, local_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    actual = (await client.get(ROLES_URL, headers=authorization_for(local_a.admin))).json()[1][
        "permissions"
    ]

    async with broker.subscribe() as queue:
        response = await client.put(
            f"{ROLES_URL}/{local_a.waiter.role.id}",
            json={"name": "Mesero", "permissions": actual},
            headers=authorization_for(local_a.admin),
        )
        await asyncio.sleep(0)

        assert response.status_code == 200
        assert queue.empty()


async def test_un_rol_con_personal_no_se_borra(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    headers = authorization_for(local_a.admin)
    cocinero = await _crear_rol(client, headers, "Cocinero", COCINA)
    await _cuenta_con_rol(client, local_a, cocinero["id"], "cocina@local-a.pe")
    vacio = await _crear_rol(client, headers, "Cajero", ["orders.charge"])

    con_personal = await client.delete(f"{ROLES_URL}/{cocinero['id']}", headers=headers)
    sin_personal = await client.delete(f"{ROLES_URL}/{vacio['id']}", headers=headers)
    quedan = await client.get(ROLES_URL, headers=headers)

    assert con_personal.status_code == 409
    assert sin_personal.status_code == 204
    assert [role["name"] for role in quedan.json()] == ["Encargado", "Mesero", "Cocinero"]
    assert quedan.json()[2]["member_count"] == 1
    assert quedan.json()[2]["is_deletable"] is False


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("PUT", "", {"name": "Intruso", "permissions": []}),
        ("DELETE", "", None),
    ],
)
async def test_un_rol_de_otro_restaurante_no_existe(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    ajeno = await _crear_rol(client, authorization_for(local_b.admin), "Cocinero", COCINA)

    response = await client.request(
        method,
        f"{ROLES_URL}/{ajeno['id']}{suffix}",
        json=body,
        headers=authorization_for(local_a.admin),
    )
    listado_a = await client.get(ROLES_URL, headers=authorization_for(local_a.admin))
    listado_b = await client.get(ROLES_URL, headers=authorization_for(local_b.admin))

    assert response.status_code == 404
    assert "Cocinero" not in {role["name"] for role in listado_a.json()}
    assert "Cocinero" in {role["name"] for role in listado_b.json()}


async def test_no_se_da_de_alta_con_un_rol_de_otro_restaurante(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    alta = await client.post(
        STAFF_URL,
        json={
            "email": "carla@local-a.pe",
            "full_name": "Carla Ríos",
            "role_id": local_b.waiter.role.id,
            "password": VALID_PASSWORD,
        },
        headers=authorization_for(local_a.admin),
    )
    cambio = await client.patch(
        f"{STAFF_URL}/{local_a.waiter.id}",
        json={"role_id": local_b.admin.role.id},
        headers=authorization_for(local_a.admin),
    )

    assert alta.status_code == 404
    assert cambio.status_code == 404


async def test_ver_los_roles_alcanza_con_gestionar_personal(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    encargado = authorization_for(local_a.admin)
    personal = await _crear_rol(client, encargado, "Personal", ["staff.manage"])
    headers = await _cuenta_con_rol(client, local_a, personal["id"], "rrhh@local-a.pe")

    listar = await client.get(ROLES_URL, headers=headers)
    catalogo = await client.get(PERMISSIONS_URL, headers=headers)
    crear = await client.post(ROLES_URL, json={"name": "X", "permissions": []}, headers=headers)

    assert listar.status_code == 200
    assert catalogo.status_code == 403
    assert crear.status_code == 403


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", PERMISSIONS_URL, None),
        ("GET", ROLES_URL, None),
        ("POST", ROLES_URL, {"name": "Cocinero", "permissions": []}),
        ("PUT", f"{ROLES_URL}/1", {"name": "Mesero", "permissions": []}),
        ("DELETE", f"{ROLES_URL}/1", None),
    ],
)
async def test_el_mesero_no_arma_roles(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    method: str,
    url: str,
    body: dict[str, object] | None,
) -> None:
    response = await client.request(
        method, url, json=body, headers=authorization_for(local_a.waiter)
    )

    assert response.status_code == 403


async def test_quien_gestiona_personal_no_toma_la_cuenta_del_encargado(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    encargado = authorization_for(local_a.admin)
    personal = await _crear_rol(client, encargado, "Personal", ["staff.manage"])
    headers = await _cuenta_con_rol(client, local_a, personal["id"], "rrhh@local-a.pe")

    clave = await client.post(
        f"{STAFF_URL}/{local_a.admin.id}/password",
        json={"new_password": "contrasena-tomada"},
        headers=headers,
    )
    degradar = await client.patch(
        f"{STAFF_URL}/{local_a.admin.id}",
        json={"role_id": local_a.waiter.role.id},
        headers=headers,
    )
    practicante = await _crear_rol(client, encargado, "Practicante", [])
    await _cuenta_con_rol(client, local_a, practicante["id"], "practica@local-a.pe")
    cuentas = await client.get(STAFF_URL, params={"search": "practica"}, headers=encargado)
    ascender = await client.patch(
        f"{STAFF_URL}/{cuentas.json()['items'][0]['id']}",
        json={"role_id": local_a.admin.role.id},
        headers=headers,
    )

    assert clave.status_code == 403
    assert degradar.status_code == 403
    assert degradar.json()["detail"] == (
        "No puedes gestionar una cuenta que tiene permisos que tú no tienes."
    )
    assert ascender.status_code == 403
    assert ascender.json()["detail"] == "No puedes dar permisos que no tienes."


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    return await carta(session, local_a.id)


async def test_el_cocinero_marca_listo_pero_no_cobra(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    cocinero = await _crear_rol(client, authorization_for(local_a.admin), "Cocinero", COCINA)
    cocina = await _cuenta_con_rol(client, local_a, cocinero["id"], "cocina@local-a.pe")
    mesero = authorization_for(local_a.waiter)
    pedido = await client.post(
        ORDERS_URL,
        json={
            "type": "dine_in",
            "table_id": carta_a.mesa_1,
            "items": [{"menu_item_id": carta_a.lomo, "quantity": 1}],
        },
        headers=mesero,
    )
    order_id = pedido.json()["id"]
    await client.post(f"{ORDERS_URL}/{order_id}/send", headers=mesero)

    tablero = await client.get(f"{ORDERS_URL}/active", headers=cocina)
    listo = await client.post(f"{ORDERS_URL}/{order_id}/ready", headers=cocina)
    await client.post(f"{ORDERS_URL}/{order_id}/served", headers=mesero)
    cobro = await client.post(
        f"{ORDERS_URL}/{order_id}/charge", json={"payment_method": "cash"}, headers=cocina
    )

    assert [order["id"] for order in tablero.json()] == [order_id]
    assert listo.status_code == 200
    assert listo.json()["status"] == "ready"
    assert cobro.status_code == 403
