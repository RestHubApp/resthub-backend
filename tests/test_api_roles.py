"""Roles por restaurante por HTTP: quién los arma, qué se puede cambiar y el aislamiento."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.permissions import CATALOG, Permission
from resthub.core.realtime import PERMISSIONS_TOPIC
from resthub.core.realtime_broker import LocalBroker
from tests.builders import SeededMenu, seed_menu
from tests.conftest import VALID_PASSWORD, StaffedRestaurant, authorization_for, url

ROLES_URL = url("roles")
PERMISSIONS_URL = url("permissions")
STAFF_URL = url("staff")
LOGIN_URL = url("auth/login")
ME_URL = url("auth/me")
OWN_PASSWORD_URL = url("auth/me/password")
ORDERS_URL = url("orders")
KITCHEN = [
    Permission.MENU_READ.value,
    Permission.ORDERS_MANAGE.value,
    Permission.ORDERS_READ_ALL.value,
]


async def _create_role(
    client: AsyncClient, headers: dict[str, str], name: str, permissions: list[str]
) -> dict[str, Any]:
    response = await client.post(
        ROLES_URL, json={"name": name, "permissions": permissions}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _account_with_role(
    client: AsyncClient, restaurant: StaffedRestaurant, role_id: int, email: str
) -> dict[str, str]:
    """Da de alta una cuenta con ese rol y devuelve su cabecera de acceso."""
    registered = await client.post(
        STAFF_URL,
        json={
            "email": email,
            "full_name": "Cuenta de prueba",
            "role_id": role_id,
            "password": VALID_PASSWORD,
        },
        headers=authorization_for(restaurant.admin),
    )
    assert registered.status_code == 201, registered.text
    login = await client.post(LOGIN_URL, json={"email": email, "password": VALID_PASSWORD})
    # La contraseña del alta es temporal: la cuenta elige la suya antes de nada.
    changed = await client.post(
        OWN_PASSWORD_URL,
        json={"current_password": VALID_PASSWORD, "new_password": "otra-clave-propia"},
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert changed.status_code == 200, changed.text
    headers = {"Authorization": f"Bearer {changed.json()['access_token']}"}
    # Las acciones críticas (roles, desactivar) piden la contraseña de nuevo (RFS-21).
    confirmed = await client.post(
        url("auth/me/reauthenticate"), json={"password": "otra-clave-propia"}, headers=headers
    )
    assert confirmed.status_code == 200, confirmed.text
    return headers


async def test_catalog_comes_grouped_with_labels(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(PERMISSIONS_URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 200
    body = response.json()
    assert {item["code"] for item in body} == {permission.value for permission in Permission}
    assert body[0] == {"code": Permission.MENU_READ.value, "label": "Ver el menú", "group": "Menú"}
    assert body[-1] == {
        "code": Permission.ROLES_MANAGE.value,
        "label": "Crear roles y elegir sus permisos",
        "group": "Administración",
    }
    groups = [item["group"] for item in body]
    assert groups == sorted(groups, key=groups.index)
    assert len(body) == len(CATALOG)


async def test_restaurant_is_born_with_owner_and_waiter(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(ROLES_URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 200
    owner, waiter = response.json()
    assert owner == {
        "id": restaurant_a.admin.role.id,
        "name": "Encargado",
        "kind": "owner",
        "permissions": owner["permissions"],
        "member_count": 1,
        "is_editable": False,
        "is_deletable": False,
    }
    assert set(owner["permissions"]) == {permission.value for permission in Permission}
    assert (waiter["name"], waiter["kind"], waiter["member_count"]) == ("Mesero", "waiter", 1)
    assert (waiter["is_editable"], waiter["is_deletable"]) == (True, False)
    assert Permission.ORDERS_CHARGE.value in waiter["permissions"]


async def test_custom_roles_come_after_and_sorted_by_name(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)
    await _create_role(client, headers, "Cocinero", KITCHEN)
    await _create_role(client, headers, "Cajero", [Permission.ORDERS_CHARGE.value])

    response = await client.get(ROLES_URL, headers=headers)

    assert [role["name"] for role in response.json()] == [
        "Encargado",
        "Mesero",
        "Cajero",
        "Cocinero",
    ]


async def test_creating_a_role_is_logged(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)

    created = await _create_role(client, headers, "  Cocinero  ", KITCHEN)
    activity_log = await client.get(
        url("activity"), params={"kind": ActivityKind.ROLE_CREATED.value}, headers=headers
    )

    assert created["name"] == "Cocinero"
    assert created["kind"] == "custom"
    assert created["permissions"] == [
        Permission.MENU_READ.value,
        Permission.ORDERS_READ_ALL.value,
        Permission.ORDERS_MANAGE.value,
    ]
    assert (created["member_count"], created["is_editable"], created["is_deletable"]) == (
        0,
        True,
        True,
    )
    assert [item["detail"] for item in activity_log.json()["items"]] == ["Cocinero (3 permisos)"]


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
async def test_validations_on_create(
    client: AsyncClient, restaurant_a: StaffedRestaurant, body: dict[str, object], status: int
) -> None:
    response = await client.post(
        ROLES_URL, json=body, headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == status, response.text


async def test_name_may_repeat_across_restaurants_but_not_within_one(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    await _create_role(client, authorization_for(restaurant_a.admin), "Cocinero", KITCHEN)

    other_restaurant = await client.post(
        ROLES_URL,
        json={"name": "Cocinero", "permissions": KITCHEN},
        headers=authorization_for(restaurant_b.admin),
    )
    duplicate = await client.post(
        ROLES_URL,
        json={"name": "cocinero", "permissions": []},
        headers=authorization_for(restaurant_a.admin),
    )

    assert other_restaurant.status_code == 201
    assert duplicate.status_code == 409


async def test_nobody_grants_permissions_they_lack(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    role_manager = await _create_role(
        client, owner, "Arma roles", [Permission.ROLES_MANAGE.value, Permission.MENU_READ.value]
    )
    headers = await _account_with_role(client, restaurant_a, role_manager["id"], "roles@local-a.pe")
    cashier = await _create_role(client, owner, "Cajero", [Permission.ORDERS_CHARGE.value])

    create = await client.post(
        ROLES_URL,
        json={"name": "Jefe", "permissions": [Permission.STAFF_MANAGE.value]},
        headers=headers,
    )
    promote_self = await client.put(
        f"{ROLES_URL}/{role_manager['id']}",
        json={
            "name": "Arma roles",
            "permissions": [
                Permission.ROLES_MANAGE.value,
                Permission.MENU_READ.value,
                Permission.CASH_MANAGE.value,
            ],
        },
        headers=headers,
    )
    trim_foreign = await client.put(
        f"{ROLES_URL}/{cashier['id']}",
        json={"name": "Cajero", "permissions": []},
        headers=headers,
    )
    allowed = await client.post(
        ROLES_URL,
        json={"name": "Solo menú", "permissions": [Permission.MENU_READ.value]},
        headers=headers,
    )

    assert create.status_code == 403
    assert create.json()["detail"] == "No puedes dar permisos que no tienes."
    assert promote_self.status_code == 403
    assert trim_foreign.status_code == 403
    assert allowed.status_code == 201


async def test_owner_role_is_neither_edited_nor_deleted(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)
    url = f"{ROLES_URL}/{restaurant_a.admin.role.id}"

    edit = await client.put(url, json={"name": "Encargado", "permissions": []}, headers=headers)
    delete = await client.delete(url, headers=headers)
    me = await client.get(ME_URL, headers=headers)

    assert edit.status_code == 409
    assert delete.status_code == 409
    assert len(me.json()["permissions"]) == len(Permission)


async def test_waiter_role_changes_permissions_but_not_name(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)
    url = f"{ROLES_URL}/{restaurant_a.waiter.role.id}"

    rename = await client.put(
        url, json={"name": "Camarero", "permissions": [Permission.MENU_READ.value]}, headers=headers
    )
    trim = await client.put(
        url,
        json={
            "name": "mesero",
            "permissions": [Permission.MENU_READ.value, Permission.ORDERS_TAKE.value],
        },
        headers=headers,
    )
    delete = await client.delete(url, headers=headers)
    me = await client.get(ME_URL, headers=authorization_for(restaurant_a.waiter))

    assert rename.status_code == 409
    assert trim.status_code == 200
    assert trim.json()["name"] == "Mesero"
    assert delete.status_code == 409
    assert me.json()["permissions"] == [Permission.MENU_READ.value, Permission.ORDERS_TAKE.value]
    assert me.json()["user"]["role_label"] == "Mesero"


async def test_changing_permissions_notifies_holders_of_the_role(
    client: AsyncClient, restaurant_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    async with broker.subscribe() as queue:
        response = await client.put(
            f"{ROLES_URL}/{restaurant_a.waiter.role.id}",
            json={"name": "Mesero", "permissions": [Permission.MENU_READ.value]},
            headers=authorization_for(restaurant_a.admin),
        )
        await asyncio.sleep(0)

        assert response.status_code == 200
        received = queue.get_nowait()
        assert queue.empty()
    assert received.topic == PERMISSIONS_TOPIC
    assert received.restaurant_id == restaurant_a.id
    assert received.user_ids == {restaurant_a.waiter.id}


async def test_saving_without_changes_does_not_notify(
    client: AsyncClient, restaurant_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    current = (await client.get(ROLES_URL, headers=authorization_for(restaurant_a.admin))).json()[
        1
    ]["permissions"]

    async with broker.subscribe() as queue:
        response = await client.put(
            f"{ROLES_URL}/{restaurant_a.waiter.role.id}",
            json={"name": "Mesero", "permissions": current},
            headers=authorization_for(restaurant_a.admin),
        )
        await asyncio.sleep(0)

        assert response.status_code == 200
        assert queue.empty()


async def test_role_with_staff_is_not_deleted(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)
    cook = await _create_role(client, headers, "Cocinero", KITCHEN)
    await _account_with_role(client, restaurant_a, cook["id"], "cocina@local-a.pe")
    empty = await _create_role(client, headers, "Cajero", [Permission.ORDERS_CHARGE.value])

    with_staff = await client.delete(f"{ROLES_URL}/{cook['id']}", headers=headers)
    without_staff = await client.delete(f"{ROLES_URL}/{empty['id']}", headers=headers)
    remaining = await client.get(ROLES_URL, headers=headers)

    assert with_staff.status_code == 409
    assert without_staff.status_code == 204
    assert [role["name"] for role in remaining.json()] == ["Encargado", "Mesero", "Cocinero"]
    assert remaining.json()[2]["member_count"] == 1
    assert remaining.json()[2]["is_deletable"] is False


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("PUT", "", {"name": "Intruso", "permissions": []}),
        ("DELETE", "", None),
    ],
)
async def test_role_from_another_restaurant_does_not_exist(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    foreign = await _create_role(client, authorization_for(restaurant_b.admin), "Cocinero", KITCHEN)

    response = await client.request(
        method,
        f"{ROLES_URL}/{foreign['id']}{suffix}",
        json=body,
        headers=authorization_for(restaurant_a.admin),
    )
    listing_a = await client.get(ROLES_URL, headers=authorization_for(restaurant_a.admin))
    listing_b = await client.get(ROLES_URL, headers=authorization_for(restaurant_b.admin))

    assert response.status_code == 404
    assert "Cocinero" not in {role["name"] for role in listing_a.json()}
    assert "Cocinero" in {role["name"] for role in listing_b.json()}


async def test_no_registration_with_a_role_from_another_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    registered = await client.post(
        STAFF_URL,
        json={
            "email": "carla@local-a.pe",
            "full_name": "Carla Ríos",
            "role_id": restaurant_b.waiter.role.id,
            "password": VALID_PASSWORD,
        },
        headers=authorization_for(restaurant_a.admin),
    )
    change = await client.patch(
        f"{STAFF_URL}/{restaurant_a.waiter.id}",
        json={"role_id": restaurant_b.admin.role.id},
        headers=authorization_for(restaurant_a.admin),
    )

    assert registered.status_code == 404
    assert change.status_code == 404


async def test_staff_management_is_enough_to_view_roles(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    staff_role = await _create_role(client, owner, "Personal", [Permission.STAFF_MANAGE.value])
    headers = await _account_with_role(client, restaurant_a, staff_role["id"], "rrhh@local-a.pe")

    listing = await client.get(ROLES_URL, headers=headers)
    catalog = await client.get(PERMISSIONS_URL, headers=headers)
    create = await client.post(ROLES_URL, json={"name": "X", "permissions": []}, headers=headers)

    assert listing.status_code == 200
    assert catalog.status_code == 403
    assert create.status_code == 403


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
async def test_waiter_cannot_build_roles(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    method: str,
    url: str,
    body: dict[str, object] | None,
) -> None:
    response = await client.request(
        method, url, json=body, headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 403


async def test_staff_manager_cannot_take_over_the_owner_account(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    staff_role = await _create_role(client, owner, "Personal", [Permission.STAFF_MANAGE.value])
    headers = await _account_with_role(client, restaurant_a, staff_role["id"], "rrhh@local-a.pe")

    password_reset = await client.post(
        f"{STAFF_URL}/{restaurant_a.admin.id}/password",
        json={"new_password": "contrasena-tomada"},
        headers=headers,
    )
    demote = await client.patch(
        f"{STAFF_URL}/{restaurant_a.admin.id}",
        json={"role_id": restaurant_a.waiter.role.id},
        headers=headers,
    )
    intern = await _create_role(client, owner, "Practicante", [])
    await _account_with_role(client, restaurant_a, intern["id"], "practica@local-a.pe")
    accounts = await client.get(STAFF_URL, params={"search": "practica@local-a.pe"}, headers=owner)
    promote = await client.patch(
        f"{STAFF_URL}/{accounts.json()['items'][0]['id']}",
        json={"role_id": restaurant_a.admin.role.id},
        headers=headers,
    )

    assert password_reset.status_code == 403
    assert demote.status_code == 403
    assert demote.json()["detail"] == (
        "No puedes gestionar una cuenta que tiene permisos que tú no tienes."
    )
    assert promote.status_code == 403
    assert promote.json()["detail"] == "No puedes dar permisos que no tienes."


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_a.id)


async def test_cook_marks_ready_but_cannot_charge(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    cook = await _create_role(client, authorization_for(restaurant_a.admin), "Cocinero", KITCHEN)
    kitchen_account = await _account_with_role(
        client, restaurant_a, cook["id"], "cocina@local-a.pe"
    )
    waiter = authorization_for(restaurant_a.waiter)
    created_order = await client.post(
        ORDERS_URL,
        json={
            "type": "dine_in",
            "table_id": menu_a.table_1,
            "items": [{"menu_item_id": menu_a.lomo, "quantity": 1}],
        },
        headers=waiter,
    )
    order_id = created_order.json()["id"]
    await client.post(f"{ORDERS_URL}/{order_id}/send", headers=waiter)

    board = await client.get(f"{ORDERS_URL}/active", headers=kitchen_account)
    marked_ready = await client.post(f"{ORDERS_URL}/{order_id}/ready", headers=kitchen_account)
    await client.post(f"{ORDERS_URL}/{order_id}/served", headers=waiter)
    charge = await client.post(
        f"{ORDERS_URL}/{order_id}/charge", json={"payment_method": "cash"}, headers=kitchen_account
    )

    assert [order["id"] for order in board.json()] == [order_id]
    assert marked_ready.status_code == 200
    assert marked_ready.json()["status"] == "ready"
    assert charge.status_code == 403
