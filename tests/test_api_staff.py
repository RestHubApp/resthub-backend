"""Gestión del personal por HTTP: permisos y aislamiento entre restaurantes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import VALID_PASSWORD, StaffedRestaurant, authorization_for, url

STAFF_URL = url("staff")
LOGIN_URL = url("auth/login")


def _new_waiter(restaurant: StaffedRestaurant) -> dict[str, object]:
    return {
        "email": "carla@local-a.pe",
        "full_name": "Carla Ríos",
        "role_id": restaurant.waiter.role.id,
        "password": VALID_PASSWORD,
    }


async def test_owner_lists_only_their_own_staff(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    response = await client.get(STAFF_URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["email"] for item in body["items"]} == {
        "encargado@local-a.pe",
        "mesero@local-a.pe",
    }
    assert {item["role_label"] for item in body["items"]} == {"Encargado", "Mesero"}


async def test_listing_filters_by_role_and_searches(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)

    waiters = await client.get(
        STAFF_URL, params={"role_id": restaurant_a.waiter.role.id}, headers=headers
    )
    search_result = await client.get(STAFF_URL, params={"search": "rosa"}, headers=headers)

    assert [item["id"] for item in waiters.json()["items"]] == [restaurant_a.waiter.id]
    assert [item["id"] for item in search_result.json()["items"]] == [restaurant_a.admin.id]


async def test_new_hire_lands_in_the_owners_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    response = await client.post(
        STAFF_URL, json=_new_waiter(restaurant_a), headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 201
    assert response.json()["role_id"] == restaurant_a.waiter.role.id
    assert response.json()["role_label"] == "Mesero"
    assert "role" not in response.json()
    login = await client.post(
        LOGIN_URL, json={"email": "carla@local-a.pe", "password": VALID_PASSWORD}
    )
    assert login.json()["restaurant"]["id"] == restaurant_a.id


async def test_restaurant_id_in_the_body_is_ignored(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    """Nunca se acepta un restaurante enviado por el cliente."""
    response = await client.post(
        STAFF_URL,
        json={**_new_waiter(restaurant_a), "restaurant_id": restaurant_b.id},
        headers=authorization_for(restaurant_a.admin),
    )
    listing_b = await client.get(STAFF_URL, headers=authorization_for(restaurant_b.admin))

    assert response.status_code == 201
    assert "carla@local-a.pe" not in {item["email"] for item in listing_b.json()["items"]}


async def test_email_from_another_restaurant_conflicts(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    response = await client.post(
        STAFF_URL,
        json={**_new_waiter(restaurant_a), "email": "mesero@local-b.pe"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 409


async def test_edit_name_and_role(client: AsyncClient, restaurant_a: StaffedRestaurant) -> None:
    response = await client.patch(
        f"{STAFF_URL}/{restaurant_a.waiter.id}",
        json={"full_name": "Luis A. Torres", "role_id": restaurant_a.admin.role.id},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Luis A. Torres"
    assert response.json()["role_id"] == restaurant_a.admin.role.id
    assert response.json()["role_label"] == "Encargado"


async def test_owner_does_not_change_their_own_role(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.patch(
        f"{STAFF_URL}/{restaurant_a.admin.id}",
        json={"role_id": restaurant_a.waiter.role.id},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 409


async def test_deactivating_cuts_off_access(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    waiter_headers = authorization_for(restaurant_a.waiter)

    response = await client.patch(
        f"{STAFF_URL}/{restaurant_a.waiter.id}/status",
        json={"is_active": False, "reason": "Terminó su contrato"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert (await client.get(url("auth/me"), headers=waiter_headers)).status_code == 401


async def test_deactivation_keeps_who_when_and_why_until_reactivated(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    target = f"{STAFF_URL}/{restaurant_a.waiter.id}/status"

    deactivated = await client.patch(
        target, json={"is_active": False, "reason": "  Terminó   su contrato "}, headers=owner
    )
    listed = await client.get(f"{STAFF_URL}/{restaurant_a.waiter.id}", headers=owner)
    reactivated = await client.patch(target, json={"is_active": True}, headers=owner)

    body = deactivated.json()
    assert (body["deactivated_by"], body["deactivation_reason"]) == (
        restaurant_a.admin.id,
        "Terminó su contrato",
    )
    assert body["deactivated_at"] is not None
    assert listed.json()["deactivation_reason"] == "Terminó su contrato"
    assert (
        reactivated.json()["deactivated_at"],
        reactivated.json()["deactivated_by"],
        reactivated.json()["deactivation_reason"],
    ) == (None, None, "")
    activity = await client.get(url("activity"), headers=owner)
    details = [item["detail"] for item in activity.json()["items"]]
    assert "Luis Torres: cuenta desactivada (Terminó su contrato)" in details


@pytest.mark.parametrize("body", [{"is_active": False}, {"is_active": False, "reason": " a "}])
async def test_deactivating_without_a_reason_responds_422(
    client: AsyncClient, restaurant_a: StaffedRestaurant, body: dict[str, object]
) -> None:
    response = await client.patch(
        f"{STAFF_URL}/{restaurant_a.waiter.id}/status",
        json=body,
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_deactivation_reason"


async def test_owner_does_not_deactivate_themselves(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.patch(
        f"{STAFF_URL}/{restaurant_a.admin.id}/status",
        json={"is_active": False, "reason": "Me voy de vacaciones"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 409


async def test_reset_a_waiters_password(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    new_password = "nueva-contrasena-larga"
    response = await client.post(
        f"{STAFF_URL}/{restaurant_a.waiter.id}/password",
        json={"new_password": new_password},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 204
    login = await client.post(
        LOGIN_URL, json={"email": "mesero@local-a.pe", "password": new_password}
    )
    assert login.status_code == 200


async def test_own_password_is_not_reset_this_way(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(
        f"{STAFF_URL}/{restaurant_a.admin.id}/password",
        json={"new_password": "nueva-contrasena-larga"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"full_name": "Intruso"}),
        ("PATCH", "/status", {"is_active": False, "reason": "Sin motivo real"}),
        ("POST", "/password", {"new_password": "contrasena-intrusa"}),
    ],
)
async def test_owner_neither_sees_nor_touches_staff_of_another_restaurant(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    response = await client.request(
        method,
        f"{STAFF_URL}/{restaurant_b.waiter.id}{suffix}",
        json=body,
        headers=authorization_for(restaurant_a.admin),
    )

    # 404 y no 403: responder "existe pero no es tuya" delataría a otros locales.
    assert response.status_code == 404
    untouched = await client.post(
        LOGIN_URL, json={"email": "mesero@local-b.pe", "password": VALID_PASSWORD}
    )
    assert untouched.status_code == 200
    assert untouched.json()["user"]["full_name"] == "Luis Torres"


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
        ("PATCH", "/{id}/status", {"is_active": False, "reason": "Sin motivo real"}),
        ("POST", "/{id}/password", {"new_password": "contrasena-intrusa"}),
    ],
)
async def test_waiter_does_not_manage_staff(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    url = STAFF_URL + suffix.replace("{id}", str(restaurant_a.admin.id))

    response = await client.request(
        method, url, json=body, headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Tu rol no tiene permiso para esta acción."


async def test_password_over_72_bytes_does_not_break_the_signup(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(
        url("staff"),
        json={
            "email": "largo@local-a.pe",
            "full_name": "Contraseña Larga",
            "role_id": restaurant_a.waiter.role.id,
            "password": "ñ" * 40,
        },
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422, response.text
