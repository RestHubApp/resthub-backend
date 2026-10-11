"""Administración del sistema por HTTP: acceso, restaurantes y bitácora."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.clock import SystemClock
from resthub.core.permissions import Permission
from resthub.modules.platform.adapters.persistence.models import PlatformAdminRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow
from tests.conftest import (
    API_PREFIX,
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    url,
)

API = API_PREFIX
LOGIN_URL = f"{API}/platform/auth/login"
ME_URL = f"{API}/platform/auth/me"
REFRESH_URL = f"{API}/platform/auth/refresh"
RESTAURANTS_URL = f"{API}/platform/restaurants"
ACTIVITY_URL = f"{API}/platform/activity"
ADMIN_EMAIL = "equipo@resthub.dev"


@pytest.fixture
async def platform_admin(session: AsyncSession) -> PlatformAdmin:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email=ADMIN_EMAIL,
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    return admin


def platform_authorization_for(admin: PlatformAdmin) -> dict[str, str]:
    token = TEST_TOKEN_SERVICE.issue_platform(admin.id or 0)
    return {"Authorization": f"Bearer {token.value}"}


@pytest.fixture
def headers(platform_admin: PlatformAdmin) -> dict[str, str]:
    return platform_authorization_for(platform_admin)


def _new_restaurant(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Cevichería Doña Rosa",
        "slug": "dona-rosa",
        "timezone": "America/Lima",
        "owner": {
            "full_name": "Rosa Pérez",
            "email": "rosa@donarosa.pe",
            "password": VALID_PASSWORD,
        },
    }
    return {**body, **changes}


async def _activity_kinds(client: AsyncClient, headers: dict[str, str]) -> list[str]:
    response = await client.get(ACTIVITY_URL, headers=headers)
    assert response.status_code == 200
    return [item["kind"] for item in response.json()["items"]]


# --- Acceso -----------------------------------------------------------------


async def test_login_returns_a_platform_token_and_the_account(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "EQUIPO@resthub.dev", "password": VALID_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["admin"] == {
        "id": platform_admin.id,
        "full_name": "Equipo RestHub",
        "email": ADMIN_EMAIL,
    }
    claims = TEST_TOKEN_SERVICE.decode_platform(body["access_token"])
    assert claims.admin_id == platform_admin.id
    payload = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert payload["scope"] == "platform"
    assert "restaurant_id" not in payload


async def test_login_is_recorded_in_the_platform_activity_log(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    login = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.get(ACTIVITY_URL, headers=headers)

    assert response.status_code == 200
    [entry] = response.json()["items"]
    assert entry["kind"] == ActivityKind.SIGNED_IN.value
    assert entry["kind_label"] == "Inició sesión"
    assert (entry["admin_id"], entry["admin_name"]) == (platform_admin.id, "Equipo RestHub")


@pytest.mark.parametrize(
    ("email", "password"),
    [(ADMIN_EMAIL, "otra-contrasena"), ("nadie@resthub.dev", VALID_PASSWORD)],
)
async def test_failed_login_responds_with_generic_401(
    client: AsyncClient, platform_admin: PlatformAdmin, email: str, password: str
) -> None:
    response = await client.post(LOGIN_URL, json={"email": email, "password": password})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_deactivated_platform_account_responds_the_same(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    await session.execute(update(PlatformAdminRow).values(is_active=False))
    await session.commit()

    response = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})

    assert response.status_code == 401
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_staff_account_cannot_log_in_through_platform_login(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "encargado@local-a.pe", "password": VALID_PASSWORD}
    )

    assert response.status_code == 401


async def test_five_failures_block_platform_login_and_not_staff_login(
    client: AsyncClient, platform_admin: PlatformAdmin, restaurant_a: StaffedRestaurant
) -> None:
    for _ in range(5):
        failure = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": "mala-mala"})
        assert failure.status_code == 401

    blocked = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})
    staff_response = await client.post(
        f"{API}/auth/login", json={"email": "encargado@local-a.pe", "password": VALID_PASSWORD}
    )

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert staff_response.status_code == 200


async def test_me_and_refresh_return_the_account(
    client: AsyncClient, platform_admin: PlatformAdmin, headers: dict[str, str]
) -> None:
    me = await client.get(ME_URL, headers=headers)
    refresh = await client.post(REFRESH_URL, headers=headers)

    assert me.status_code == 200
    assert me.json() == {
        "admin": {"id": platform_admin.id, "full_name": "Equipo RestHub", "email": ADMIN_EMAIL}
    }
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["admin"] == me.json()["admin"]
    assert TEST_TOKEN_SERVICE.decode_platform(body["access_token"]).admin_id == platform_admin.id
    renewed = await client.get(ME_URL, headers={"Authorization": f"Bearer {body['access_token']}"})
    assert renewed.status_code == 200


async def test_me_without_credentials_responds_401(client: AsyncClient) -> None:
    response = await client.get(ME_URL)

    assert response.status_code == 401


async def test_deactivating_the_account_cuts_an_already_issued_token(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    await session.execute(update(PlatformAdminRow).values(is_active=False))
    await session.commit()

    response = await client.get(ME_URL, headers=headers)

    assert response.status_code == 401


# --- Un token no sirve del otro lado ------------------------------------------


@pytest.mark.parametrize("path", ["/restaurant", "/staff", "/orders"])
async def test_platform_token_is_not_valid_in_a_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, headers: dict[str, str], path: str
) -> None:
    response = await client.get(f"{API}{path}", headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path", ["/platform/auth/me", "/platform/restaurants", "/platform/activity"]
)
async def test_restaurant_token_is_not_valid_on_the_platform(
    client: AsyncClient, platform_admin: PlatformAdmin, restaurant_a: StaffedRestaurant, path: str
) -> None:
    # El encargado tiene todos los permisos del local, y aun así no pasa.
    response = await client.get(f"{API}{path}", headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 401


async def test_restaurant_token_without_scope_still_works_only_in_the_restaurant(
    client: AsyncClient, platform_admin: PlatformAdmin, restaurant_a: StaffedRestaurant
) -> None:
    """Los tokens emitidos antes de la plataforma no traen `scope`."""
    now = SystemClock().now()
    token = jwt.encode(
        {
            "sub": str(restaurant_a.admin.id),
            "restaurant_id": restaurant_a.id,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        "secreto-de-prueba-con-largo-suficiente",
        algorithm="HS256",
    )
    legacy_headers = {"Authorization": f"Bearer {token}"}
    # Mismo número de cuenta en las dos tablas: solo el alcance las separa.
    assert restaurant_a.admin.id == platform_admin.id

    restaurant_response = await client.get(f"{API}/auth/me", headers=legacy_headers)
    platform_response = await client.get(ME_URL, headers=legacy_headers)

    assert restaurant_response.status_code == 200
    assert platform_response.status_code == 401


# --- Restaurantes ---------------------------------------------------------------


async def test_lists_restaurants_with_their_staff_newest_first(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    await session.execute(
        text("UPDATE users SET is_active = :inactive WHERE id = :id"),
        {"inactive": False, "id": restaurant_b.waiter.id},
    )
    await session.execute(
        update(RestaurantRow)
        .where(RestaurantRow.id == restaurant_a.id)
        .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await session.commit()

    response = await client.get(RESTAURANTS_URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["slug"] for item in body["items"]] == ["local-b", "local-a"]
    first_item = body["items"][0]
    assert set(first_item) == {
        "id",
        "name",
        "slug",
        "timezone",
        "is_active",
        "created_at",
        "staff_count",
        "active_staff_count",
    }
    assert (first_item["staff_count"], first_item["active_staff_count"]) == (2, 1)
    assert (first_item["timezone"], first_item["is_active"]) == ("America/Lima", True)


@pytest.mark.parametrize(("search", "slugs"), [("LOCAL-A", ["local-a"]), ("restaurante", None)])
async def test_search_by_name_or_slug_ignores_case(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    headers: dict[str, str],
    search: str,
    slugs: list[str] | None,
) -> None:
    response = await client.get(RESTAURANTS_URL, params={"search": search}, headers=headers)

    body = response.json()
    if slugs is None:
        assert body["total"] == 2
    else:
        assert [item["slug"] for item in body["items"]] == slugs
        assert body["total"] == 1


async def test_wildcard_in_search_is_matched_literally(
    client: AsyncClient, restaurant_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.get(RESTAURANTS_URL, params={"search": "%"}, headers=headers)

    assert response.json() == {"items": [], "total": 0}


async def test_restaurants_are_paginated(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    first_page = await client.get(RESTAURANTS_URL, params={"limit": 1}, headers=headers)
    second_page = await client.get(
        RESTAURANTS_URL, params={"limit": 1, "offset": 1}, headers=headers
    )

    assert first_page.json()["total"] == second_page.json()["total"] == 2
    ids = [first_page.json()["items"][0]["id"], second_page.json()["items"][0]["id"]]
    assert sorted(ids) == sorted([restaurant_a.id, restaurant_b.id])


async def test_onboarding_creates_restaurant_base_roles_and_owner(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    response = await client.post(RESTAURANTS_URL, json=_new_restaurant(), headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["slug"], body["timezone"]) == (
        "Cevichería Doña Rosa",
        "dona-rosa",
        "America/Lima",
    )
    assert (body["is_active"], body["staff_count"], body["active_staff_count"]) == (True, 1, 1)
    [owner] = body["owners"]
    assert owner["email"] == "rosa@donarosa.pe"
    assert (owner["full_name"], owner["is_active"]) == ("Rosa Pérez", True)
    roles = (
        await session.execute(
            text("SELECT name, kind FROM roles WHERE restaurant_id = :id ORDER BY kind"),
            {"id": body["id"]},
        )
    ).all()
    assert [tuple(role) for role in roles] == [("Encargado", "owner"), ("Mesero", "waiter")]


async def test_created_owner_logs_in_with_all_permissions(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    created = await client.post(RESTAURANTS_URL, json=_new_restaurant(), headers=headers)

    login = await client.post(
        f"{API}/auth/login", json={"email": "rosa@donarosa.pe", "password": VALID_PASSWORD}
    )

    assert login.status_code == 200
    body = login.json()
    assert body["restaurant"]["id"] == created.json()["id"]
    assert body["user"]["role_label"] == "Encargado"
    assert body["permissions"] == sorted(permission.value for permission in Permission)


async def test_onboarding_is_recorded_in_the_activity_log(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    await client.post(RESTAURANTS_URL, json=_new_restaurant(), headers=headers)

    response = await client.get(ACTIVITY_URL, headers=headers)

    [entry] = response.json()["items"]
    assert entry["kind"] == "restaurant_created"
    assert "dona-rosa" in entry["detail"]
    assert "rosa@donarosa.pe" in entry["detail"]


async def test_duplicate_slug_responds_409(
    client: AsyncClient, restaurant_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.post(
        RESTAURANTS_URL, json=_new_restaurant(slug="local-a"), headers=headers
    )

    assert response.status_code == 409


async def test_used_email_responds_409_and_leaves_no_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    owner = {"full_name": "Otra", "email": "mesero@local-a.pe", "password": VALID_PASSWORD}

    response = await client.post(
        RESTAURANTS_URL, json=_new_restaurant(owner=owner), headers=headers
    )
    listing = await client.get(RESTAURANTS_URL, params={"search": "dona"}, headers=headers)

    assert response.status_code == 409
    # Una sola transacción: sin encargado no queda un restaurante a medias.
    assert listing.json()["total"] == 0
    assert await _activity_kinds(client, headers) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "America/Atlantida"},
        {"slug": "Doña Rosa"},
        {"slug": "-dona-"},
        # Reservados para el local de muestra.
        {"slug": "muestra"},
        {"slug": "muestra-0a1b2c3d"},
        {
            "owner": {
                "full_name": "Rosa",
                "email": "rosa@donarosa.pe",
                "password": "corta",
            }
        },
    ],
)
async def test_invalid_data_responds_422(
    client: AsyncClient, headers: dict[str, str], changes: dict[str, object]
) -> None:
    response = await client.post(RESTAURANTS_URL, json=_new_restaurant(**changes), headers=headers)
    listing = await client.get(RESTAURANTS_URL, headers=headers)

    assert response.status_code == 422
    assert listing.json()["total"] == 0


async def test_profile_includes_only_the_owners(
    client: AsyncClient, restaurant_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.get(f"{RESTAURANTS_URL}/{restaurant_a.id}", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "local-a"
    assert body["staff_count"] == 2
    assert body["owners"] == [
        {
            "id": restaurant_a.admin.id,
            "full_name": "Rosa Pérez",
            "email": "encargado@local-a.pe",
            "is_active": True,
        }
    ]


async def test_profile_of_a_nonexistent_restaurant_responds_404(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    profile = await client.get(f"{RESTAURANTS_URL}/999", headers=headers)
    edit_response = await client.patch(
        f"{RESTAURANTS_URL}/999", json={"name": "X"}, headers=headers
    )
    owner_response = await client.post(
        f"{RESTAURANTS_URL}/999/owners",
        json={"full_name": "X", "email": "x@x.pe", "password": VALID_PASSWORD},
        headers=headers,
    )

    assert (profile.status_code, edit_response.status_code, owner_response.status_code) == (
        404,
        404,
        404,
    )


async def test_edits_name_and_timezone(
    client: AsyncClient, restaurant_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.patch(
        f"{RESTAURANTS_URL}/{restaurant_a.id}",
        json={"name": "  Nuevo   nombre ", "timezone": "America/Bogota"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["timezone"], body["is_active"]) == (
        "Nuevo nombre",
        "America/Bogota",
        True,
    )
    assert len(body["owners"]) == 1
    [entry] = (await client.get(ACTIVITY_URL, headers=headers)).json()["items"]
    assert entry["kind"] == ActivityKind.RESTAURANT_UPDATED.value
    assert entry["detail"] == "local-a: nombre «Nuevo nombre», zona horaria America/Bogota"


async def test_editing_with_an_invalid_timezone_responds_422(
    client: AsyncClient, restaurant_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.patch(
        f"{RESTAURANTS_URL}/{restaurant_a.id}", json={"timezone": "Lima"}, headers=headers
    )

    assert response.status_code == 422


async def test_deactivating_cuts_staff_access_and_reactivating_restores_it(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    owner_auth, waiter_auth = (
        authorization_for(restaurant_a.admin),
        authorization_for(restaurant_a.waiter),
    )
    other_restaurant_auth = authorization_for(restaurant_b.admin)

    deactivated = await client.patch(
        f"{RESTAURANTS_URL}/{restaurant_a.id}", json={"is_active": False}, headers=headers
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    assert (await client.get(f"{API}/auth/me", headers=owner_auth)).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers=waiter_auth)).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers=other_restaurant_auth)).status_code == 200

    activated = await client.patch(
        f"{RESTAURANTS_URL}/{restaurant_a.id}", json={"is_active": True}, headers=headers
    )
    assert activated.json()["is_active"] is True
    assert (await client.get(f"{API}/auth/me", headers=owner_auth)).status_code == 200

    details = [
        item["detail"] for item in (await client.get(ACTIVITY_URL, headers=headers)).json()["items"]
    ]
    assert details == ["local-a: activado", "local-a: desactivado"]


async def test_editing_without_changes_leaves_no_entry(
    client: AsyncClient, restaurant_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.patch(
        f"{RESTAURANTS_URL}/{restaurant_a.id}", json={"is_active": True}, headers=headers
    )

    assert response.status_code == 200
    assert await _activity_kinds(client, headers) == []


async def test_adds_another_owner_who_can_log_in(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    response = await client.post(
        f"{RESTAURANTS_URL}/{restaurant_a.id}/owners",
        json={"full_name": "Carla Ríos", "email": "Carla@Local-A.pe", "password": VALID_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["full_name"], body["email"], body["is_active"]) == (
        "Carla Ríos",
        "carla@local-a.pe",
        True,
    )
    profile = await client.get(f"{RESTAURANTS_URL}/{restaurant_a.id}", headers=headers)
    assert {owner["email"] for owner in profile.json()["owners"]} == {
        "encargado@local-a.pe",
        "carla@local-a.pe",
    }
    login = await client.post(
        f"{API}/auth/login", json={"email": "carla@local-a.pe", "password": VALID_PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["restaurant"]["id"] == restaurant_a.id
    assert Permission.STAFF_MANAGE.value in login.json()["permissions"]
    [entry] = (await client.get(ACTIVITY_URL, headers=headers)).json()["items"]
    assert (entry["kind"], entry["detail"]) == ("owner_added", "carla@local-a.pe en local-a")


async def test_adding_an_owner_with_a_used_email_responds_409(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    response = await client.post(
        f"{RESTAURANTS_URL}/{restaurant_a.id}/owners",
        json={"full_name": "Luis", "email": "mesero@local-b.pe", "password": VALID_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 409


# --- Bitácora -----------------------------------------------------------------


async def test_activity_log_is_paginated_newest_first(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    for _ in range(3):
        await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})
    headers = platform_authorization_for(platform_admin)
    await client.post(RESTAURANTS_URL, json=_new_restaurant(), headers=headers)

    response = await client.get(ACTIVITY_URL, params={"limit": 2}, headers=headers)

    body = response.json()
    assert body["total"] == 4
    assert [item["kind"] for item in body["items"]] == [
        "restaurant_created",
        ActivityKind.SIGNED_IN.value,
    ]
    assert set(body["items"][0]) == {
        "id",
        "admin_id",
        "admin_name",
        "kind",
        "kind_label",
        "detail",
        "created_at",
    }
    ids = (await session.execute(select(PlatformAdminRow.id))).scalars().all()
    assert ids == [platform_admin.id]


async def test_password_over_72_bytes_is_rejected_without_500(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    body = {
        "name": "Local largo",
        "slug": "local-largo",
        "timezone": "America/Lima",
        # 30 caracteres, pero 120 bytes: bcrypt no los acepta.
        "owner": {"full_name": "Ana Paz", "email": "ana@largo.pe", "password": "🍋" * 30},
    }

    response = await client.post(url("platform/restaurants"), json=body, headers=headers)

    assert response.status_code == 422, response.text


async def test_out_of_range_id_is_422_not_500(client: AsyncClient, headers: dict[str, str]) -> None:
    too_big = await client.get(url("platform/restaurants/3000000000"), headers=headers)
    too_far = await client.get(
        url("platform/restaurants"), params={"offset": 10**20}, headers=headers
    )

    assert too_big.status_code == 422
    assert too_far.status_code == 422
