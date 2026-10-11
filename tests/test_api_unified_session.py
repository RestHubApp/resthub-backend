"""Acceso único y sesión discriminada por `kind` (personal o plataforma)."""

from __future__ import annotations

import jwt
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import Permission, PlatformPermission
from resthub.modules.platform.adapters.api.dependencies import get_admin_permissions
from resthub.modules.platform.adapters.persistence.directories import SqlAlchemyStaffEmailDirectory
from resthub.modules.platform.adapters.persistence.models import PlatformAdminRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.exceptions import EmailAlreadyRegisteredError
from resthub.modules.platform.use_cases.manage_admins import RegisterAdmin, RegisterAdminCommand
from tests.conftest import (
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    url,
)

LOGIN_URL = url("auth/login")
ME_URL = url("auth/me")
REFRESH_URL = url("auth/refresh")
ADMIN_EMAIL = "equipo@resthub.dev"
EVERY_PLATFORM_PERMISSION = [permission.value for permission in PlatformPermission]


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


def _platform_headers(admin: PlatformAdmin) -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN_SERVICE.issue_platform(admin.id or 0).value}"}


async def test_login_of_a_platform_account_returns_the_platform_session(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "EQUIPO@resthub.dev", "password": VALID_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "platform"
    assert body["admin"] == {
        "id": platform_admin.id,
        "email": ADMIN_EMAIL,
        "full_name": "Equipo RestHub",
    }
    assert body["permissions"] == EVERY_PLATFORM_PERMISSION
    assert body["preview"] is False
    assert (body["token_type"], body["expires_in"]) == ("bearer", 3600)
    assert "restaurant" not in body
    assert "user" not in body
    # El token conserva su alcance propio.
    payload = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert payload["scope"] == "platform"
    assert TEST_TOKEN_SERVICE.decode_platform(body["access_token"]).admin_id == platform_admin.id


async def test_login_of_a_staff_account_returns_the_staff_session(
    client: AsyncClient, restaurant_a: StaffedRestaurant, platform_admin: PlatformAdmin
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "encargado@local-a.pe", "password": VALID_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "staff"
    assert body["restaurant"]["id"] == restaurant_a.id
    assert Permission.STAFF_MANAGE.value in body["permissions"]
    assert body["preview"] is False
    assert "admin" not in body
    assert TEST_TOKEN_SERVICE.decode(body["access_token"]).restaurant_id == restaurant_a.id


async def test_staff_session_never_includes_platform_permissions(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(ME_URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 200
    assert response.json()["kind"] == "staff"
    assert not set(response.json()["permissions"]) & set(EVERY_PLATFORM_PERMISSION)
    assert not {permission.value for permission in Permission} & set(EVERY_PLATFORM_PERMISSION)


@pytest.mark.parametrize(
    ("email", "password"),
    [(ADMIN_EMAIL, "otra-contrasena"), ("nadie@resthub.dev", VALID_PASSWORD)],
)
async def test_failed_login_responds_the_same_generic_401(
    client: AsyncClient, platform_admin: PlatformAdmin, email: str, password: str
) -> None:
    response = await client.post(LOGIN_URL, json={"email": email, "password": password})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_deactivated_platform_account_cannot_log_in(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    await session.execute(update(PlatformAdminRow).values(is_active=False))
    await session.commit()

    response = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})

    assert response.status_code == 401


async def test_failures_of_both_kinds_share_the_staff_counter(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    for _ in range(5):
        failure = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": "mala-mala"})
        assert failure.status_code == 401

    blocked = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


async def test_me_and_refresh_return_the_platform_variant(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    headers = _platform_headers(platform_admin)

    me = await client.get(ME_URL, headers=headers)
    refresh = await client.post(REFRESH_URL, headers=headers)

    assert me.status_code == 200
    assert me.json() == {
        "kind": "platform",
        "admin": {"id": platform_admin.id, "email": ADMIN_EMAIL, "full_name": "Equipo RestHub"},
        "permissions": EVERY_PLATFORM_PERMISSION,
        "preview": False,
    }
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["kind"] == "platform"
    assert TEST_TOKEN_SERVICE.decode_platform(body["access_token"]).admin_id == platform_admin.id
    renewed = await client.get(ME_URL, headers={"Authorization": f"Bearer {body['access_token']}"})
    assert renewed.status_code == 200


async def test_refresh_of_a_staff_token_returns_the_staff_variant(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(REFRESH_URL, headers=authorization_for(restaurant_a.waiter))

    assert response.status_code == 200
    assert response.json()["kind"] == "staff"
    assert TEST_TOKEN_SERVICE.decode(response.json()["access_token"]).restaurant_id == (
        restaurant_a.id
    )


async def test_deactivated_platform_account_loses_me_and_refresh(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    headers = _platform_headers(platform_admin)
    await session.execute(update(PlatformAdminRow).values(is_active=False))
    await session.commit()

    assert (await client.get(ME_URL, headers=headers)).status_code == 401
    assert (await client.post(REFRESH_URL, headers=headers)).status_code == 401


@pytest.mark.parametrize("credential", [None, "no-es-un-token"])
async def test_me_without_a_valid_credential_responds_401(
    client: AsyncClient, credential: str | None
) -> None:
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}

    response = await client.get(ME_URL, headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "auth/me/terms", {"version": "1"}),
        ("POST", "auth/me/password", {"current_password": "x", "new_password": "y" * 12}),
        ("GET", "restaurant", None),
        ("GET", "staff", None),
        ("GET", "orders", None),
    ],
)
async def test_platform_token_is_still_rejected_by_staff_only_endpoints(
    client: AsyncClient,
    platform_admin: PlatformAdmin,
    restaurant_a: StaffedRestaurant,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    response = await client.request(
        method, url(path), headers=_platform_headers(platform_admin), json=body
    )

    assert response.status_code == 401


async def test_restaurant_token_is_still_rejected_on_the_platform(
    client: AsyncClient, platform_admin: PlatformAdmin, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(
        url("platform/restaurants"), headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 401


async def test_deprecated_platform_auth_routes_still_work(
    client: AsyncClient, app: FastAPI, platform_admin: PlatformAdmin
) -> None:
    login = await client.post(
        url("platform/auth/login"), json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD}
    )
    paths = app.openapi()["paths"]

    assert login.status_code == 200
    assert login.json()["admin"]["email"] == ADMIN_EMAIL
    for route, method in [("login", "post"), ("refresh", "post"), ("me", "get")]:
        assert paths[url(f"platform/auth/{route}")][method]["deprecated"] is True
    assert "deprecated" not in paths[url("auth/login")]["post"]


async def test_openapi_discriminates_the_session_by_kind(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]

    for route, method in [("auth/login", "post"), ("auth/refresh", "post"), ("auth/me", "get")]:
        body = paths[url(route)][method]["responses"]["200"]["content"]["application/json"]
        assert body["schema"]["discriminator"]["propertyName"] == "kind"
        assert set(body["schema"]["discriminator"]["mapping"]) == {"staff", "platform"}
    assert {"StaffAccessTokenResponse", "PlatformAdminAccessTokenResponse"} <= set(
        schema["components"]["schemas"]
    )


# --- Un correo no puede estar en las dos tablas --------------------------------


async def test_platform_admin_registration_rejects_a_staff_email(
    session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    register = RegisterAdmin(
        SqlAlchemyPlatformAdminRepository(session),
        TEST_HASHER,
        SqlAlchemyStaffEmailDirectory(session),
    )

    with pytest.raises(EmailAlreadyRegisteredError):
        await register(
            RegisterAdminCommand(
                email="ENCARGADO@local-a.pe", full_name="Otro", password=VALID_PASSWORD
            )
        )
    created = await register(
        RegisterAdminCommand(email="nuevo@resthub.dev", full_name="Nuevo", password=VALID_PASSWORD)
    )
    assert created.id is not None


async def test_staff_registration_rejects_a_platform_email(
    client: AsyncClient, restaurant_a: StaffedRestaurant, platform_admin: PlatformAdmin
) -> None:
    response = await client.post(
        url("staff"),
        headers=authorization_for(restaurant_a.admin),
        json={
            "email": ADMIN_EMAIL,
            "full_name": "Intruso",
            "role_id": restaurant_a.waiter.role.id,
            "password": VALID_PASSWORD,
        },
    )

    assert response.status_code == 409


# --- 403 por permiso de plataforma ---------------------------------------------

PLATFORM_ENDPOINTS: list[tuple[str, str, PlatformPermission]] = [
    ("GET", "platform/restaurants", PlatformPermission.RESTAURANTS_READ),
    ("GET", "platform/restaurants/1", PlatformPermission.RESTAURANTS_READ),
    ("POST", "platform/restaurants", PlatformPermission.RESTAURANTS_MANAGE),
    ("PATCH", "platform/restaurants/1", PlatformPermission.RESTAURANTS_MANAGE),
    ("POST", "platform/restaurants/1/owners", PlatformPermission.RESTAURANTS_MANAGE),
    ("GET", "platform/activity", PlatformPermission.ACTIVITY_READ),
    ("GET", "platform/sandbox", PlatformPermission.PREVIEW_OPEN),
    ("POST", "platform/sandbox/reset", PlatformPermission.PREVIEW_OPEN),
    ("POST", "platform/preview", PlatformPermission.PREVIEW_OPEN),
    ("GET", "platform/observability/summary", PlatformPermission.OBSERVABILITY_READ),
    ("GET", "platform/observability/timeseries", PlatformPermission.OBSERVABILITY_READ),
    ("GET", "platform/observability/routes", PlatformPermission.OBSERVABILITY_READ),
    ("GET", "platform/observability/logs", PlatformPermission.OBSERVABILITY_READ),
    ("GET", "platform/observability/logs/1", PlatformPermission.OBSERVABILITY_READ),
    ("GET", "platform/observability/requests", PlatformPermission.OBSERVABILITY_READ),
]


@pytest.mark.parametrize(("method", "path", "permission"), PLATFORM_ENDPOINTS)
async def test_platform_endpoint_requires_its_permission(
    client: AsyncClient,
    app: FastAPI,
    platform_admin: PlatformAdmin,
    method: str,
    path: str,
    permission: PlatformPermission,
) -> None:
    headers = _platform_headers(platform_admin)
    # Con todos menos el suyo: 403. Con solo el suyo: pasa el permiso (no es un 403).
    app.dependency_overrides[get_admin_permissions] = lambda: (
        frozenset(PlatformPermission) - {permission}
    )
    denied = await client.request(method, url(path), headers=headers, json={})
    app.dependency_overrides[get_admin_permissions] = lambda: frozenset({permission})
    allowed = await client.request(method, url(path), headers=headers, json={})

    assert denied.status_code == 403
    assert denied.json()["code"] == "missing_permission"
    assert allowed.status_code != 403
