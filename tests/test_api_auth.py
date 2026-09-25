"""Acceso, sesión propia y cambio de contraseña por HTTP."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.identity import Role
from resthub.modules.accounts.adapters.persistence.models import UserRow
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow
from tests.conftest import (
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
)

LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/auth/me"
PASSWORD_URL = "/api/v1/auth/me/password"


async def test_login_devuelve_el_token_y_la_sesion(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "MESERO@local-a.pe", "password": VALID_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    claims = TEST_TOKEN_SERVICE.decode(body["access_token"])
    assert (claims.user_id, claims.role, claims.restaurant_id) == (
        local_a.waiter.id,
        Role.WAITER,
        local_a.id,
    )
    assert body["user"]["email"] == "mesero@local-a.pe"
    assert body["restaurant"] == {
        "id": local_a.id,
        "name": "Restaurante local-a",
        "slug": "local-a",
        "timezone": "America/Lima",
    }
    assert body["permissions"] == ["menu.read", "orders.take", "tables.read"]


async def test_una_contrasena_equivocada_responde_401(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "mesero@local-a.pe", "password": "otra-contrasena"}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_un_correo_desconocido_responde_igual(client: AsyncClient) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "nadie@example.com", "password": VALID_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_una_cuenta_desactivada_responde_403(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    await session.execute(
        update(UserRow).where(UserRow.id == local_a.waiter.id).values(is_active=False)
    )
    await session.commit()

    response = await client.post(
        LOGIN_URL, json={"email": "mesero@local-a.pe", "password": VALID_PASSWORD}
    )

    assert response.status_code == 403


async def test_un_restaurante_desactivado_corta_el_acceso_de_su_personal(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    headers = authorization_for(local_a.admin)
    await session.execute(
        update(RestaurantRow).where(RestaurantRow.id == local_a.id).values(is_active=False)
    )
    await session.commit()

    login = await client.post(
        LOGIN_URL, json={"email": "encargado@local-a.pe", "password": VALID_PASSWORD}
    )
    me = await client.get(ME_URL, headers=headers)

    assert login.status_code == 403
    # Un token emitido antes del corte tampoco sirve.
    assert me.status_code == 401


async def test_me_devuelve_usuario_restaurante_y_permisos(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.get(ME_URL, headers=authorization_for(local_a.admin))

    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {
        "id": local_a.admin.id,
        "full_name": "Rosa Pérez",
        "email": "encargado@local-a.pe",
        "role": "admin",
        "role_label": "Encargado",
    }
    assert body["restaurant"] == {
        "id": local_a.id,
        "name": "Restaurante local-a",
        "slug": "local-a",
        "timezone": "America/Lima",
    }
    assert "staff.manage" in body["permissions"]
    assert body["permissions"] == sorted(body["permissions"])


async def test_me_sin_credencial_responde_401(client: AsyncClient) -> None:
    response = await client.get(ME_URL)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_un_token_con_otro_restaurante_se_rechaza(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    """Si el token dice otro restaurante que la base, gana la base y el token no sirve."""
    forjado = TEST_TOKEN_SERVICE.issue(local_a.admin.id or 0, Role.ADMIN, local_b.id)

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {forjado.value}"})

    assert response.status_code == 401


async def test_el_rol_se_relee_de_la_base_y_no_del_token(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    headers = authorization_for(local_a.waiter)
    await session.execute(
        update(UserRow).where(UserRow.id == local_a.waiter.id).values(role=Role.ADMIN.value)
    )
    await session.commit()

    response = await client.get(ME_URL, headers=headers)

    assert response.json()["user"]["role"] == "admin"
    assert "staff.manage" in response.json()["permissions"]


async def test_cambiar_la_contrasena_propia(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    nueva = "otra-contrasena-larga"
    response = await client.post(
        PASSWORD_URL,
        json={"current_password": VALID_PASSWORD, "new_password": nueva},
        headers=authorization_for(local_a.waiter),
    )

    assert response.status_code == 204
    vieja = await client.post(
        LOGIN_URL, json={"email": "mesero@local-a.pe", "password": VALID_PASSWORD}
    )
    assert vieja.status_code == 401
    ok = await client.post(LOGIN_URL, json={"email": "mesero@local-a.pe", "password": nueva})
    assert ok.status_code == 200


async def test_cambiar_la_contrasena_exige_la_actual(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.post(
        PASSWORD_URL,
        json={"current_password": "no-es-la-actual", "new_password": "otra-contrasena-larga"},
        headers=authorization_for(local_a.waiter),
    )

    assert response.status_code == 400


async def test_una_contrasena_nueva_corta_se_rechaza(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.post(
        PASSWORD_URL,
        json={"current_password": VALID_PASSWORD, "new_password": "corta"},
        headers=authorization_for(local_a.waiter),
    )

    assert response.status_code == 422
