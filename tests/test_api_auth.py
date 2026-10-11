"""Acceso, sesión propia y cambio de contraseña por HTTP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.permissions import Permission
from resthub.modules.accounts.adapters.persistence.models import UserRow
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow
from tests.conftest import (
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    url,
)

LOGIN_URL = url("auth/login")
ME_URL = url("auth/me")
PASSWORD_URL = url("auth/me/password")


async def test_login_returns_token_and_session(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "MESERO@local-a.pe", "password": VALID_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    claims = TEST_TOKEN_SERVICE.decode(body["access_token"])
    assert (claims.user_id, claims.restaurant_id) == (restaurant_a.waiter.id, restaurant_a.id)
    assert body["user"]["email"] == "mesero@local-a.pe"
    assert (body["user"]["role_id"], body["user"]["role_label"]) == (
        restaurant_a.waiter.role.id,
        "Mesero",
    )
    assert body["restaurant"] == {
        "id": restaurant_a.id,
        "name": "Restaurante local-a",
        "slug": "local-a",
        "timezone": "America/Lima",
    }
    assert body["permissions"] == [
        Permission.BILLING_ISSUE.value,
        Permission.CUSTOMERS_MANAGE.value,
        Permission.CUSTOMERS_READ.value,
        Permission.MENU_READ.value,
        Permission.ORDERS_CHARGE.value,
        Permission.ORDERS_TAKE.value,
        Permission.RESERVATIONS_MANAGE.value,
        Permission.RESERVATIONS_READ.value,
        Permission.TABLES_READ.value,
    ]


async def test_wrong_password_returns_401(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "mesero@local-a.pe", "password": "otra-contrasena"}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_unknown_email_returns_same_response(client: AsyncClient) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "nadie@example.com", "password": VALID_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_deactivated_account_responds_like_wrong_password(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    await session.execute(
        update(UserRow).where(UserRow.id == restaurant_a.waiter.id).values(is_active=False)
    )
    await session.commit()

    response = await client.post(
        LOGIN_URL, json={"email": "mesero@local-a.pe", "password": VALID_PASSWORD}
    )

    # Un 403 confirmaría que la contraseña es la correcta.
    assert response.status_code == 401
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_deactivated_restaurant_cuts_off_staff_access(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)
    await session.execute(
        update(RestaurantRow).where(RestaurantRow.id == restaurant_a.id).values(is_active=False)
    )
    await session.commit()

    login = await client.post(
        LOGIN_URL, json={"email": "encargado@local-a.pe", "password": VALID_PASSWORD}
    )
    me = await client.get(ME_URL, headers=headers)

    assert login.status_code == 403
    # Un token emitido antes del corte tampoco sirve.
    assert me.status_code == 401


async def test_me_returns_user_restaurant_and_permissions(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(ME_URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {
        "id": restaurant_a.admin.id,
        "full_name": "Rosa Pérez",
        "email": "encargado@local-a.pe",
        "role_id": restaurant_a.admin.role.id,
        "role_label": "Encargado",
    }
    assert body["restaurant"] == {
        "id": restaurant_a.id,
        "name": "Restaurante local-a",
        "slug": "local-a",
        "timezone": "America/Lima",
    }
    assert Permission.STAFF_MANAGE.value in body["permissions"]
    assert body["permissions"] == sorted(body["permissions"])


async def test_me_without_credential_returns_401(client: AsyncClient) -> None:
    response = await client.get(ME_URL)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_token_with_other_restaurant_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    """Si el token dice otro restaurante que la base, gana la base y el token no sirve."""
    forged = TEST_TOKEN_SERVICE.issue(restaurant_a.admin.id or 0, restaurant_b.id)

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {forged.value}"})

    assert response.status_code == 401


async def test_role_is_reread_from_database_not_from_token(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.waiter)
    await session.execute(
        update(UserRow)
        .where(UserRow.id == restaurant_a.waiter.id)
        .values(role_id=restaurant_a.admin.role.id)
    )
    await session.commit()

    response = await client.get(ME_URL, headers=headers)

    assert response.json()["user"]["role_label"] == "Encargado"
    assert Permission.STAFF_MANAGE.value in response.json()["permissions"]


async def test_change_own_password(client: AsyncClient, restaurant_a: StaffedRestaurant) -> None:
    new_password = "otra-contrasena-larga"
    response = await client.post(
        PASSWORD_URL,
        json={"current_password": VALID_PASSWORD, "new_password": new_password},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 200
    old = await client.post(
        LOGIN_URL, json={"email": "mesero@local-a.pe", "password": VALID_PASSWORD}
    )
    assert old.status_code == 401
    ok = await client.post(LOGIN_URL, json={"email": "mesero@local-a.pe", "password": new_password})
    assert ok.status_code == 200


async def test_changing_password_cuts_previous_sessions(
    client: AsyncClient, restaurant_a: StaffedRestaurant, session: AsyncSession
) -> None:
    headers = authorization_for(restaurant_a.waiter)
    assert (await client.get(ME_URL, headers=headers)).status_code == 200

    await client.post(
        PASSWORD_URL,
        json={"current_password": VALID_PASSWORD, "new_password": "otra-contrasena-larga"},
        headers=headers,
    )
    row = await session.get(UserRow, restaurant_a.waiter.id)
    assert row is not None
    assert row.sessions_valid_since is not None

    # Un token emitido antes del cambio ya no sirve; el segundo en que cambió
    # se adelanta para no depender de la hora del reloj.
    row.sessions_valid_since = datetime.now(UTC) + timedelta(hours=1)
    await session.flush()
    rejected = await client.get(ME_URL, headers=headers)

    assert rejected.status_code == 401


async def test_changing_password_requires_current_one(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(
        PASSWORD_URL,
        json={"current_password": "no-es-la-actual", "new_password": "otra-contrasena-larga"},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 400


async def test_short_new_password_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.post(
        PASSWORD_URL,
        json={"current_password": VALID_PASSWORD, "new_password": "corta"},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 422


async def test_five_wrong_passwords_lock_that_email(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    body = {"email": restaurant_a.waiter.email, "password": "no-es-esta"}
    for _ in range(5):
        failed = await client.post(url("auth/login"), json=body)
        assert failed.status_code == 401

    blocked = await client.post(url("auth/login"), json={**body, "password": VALID_PASSWORD})
    other_email = await client.post(
        url("auth/login"), json={"email": restaurant_a.admin.email, "password": VALID_PASSWORD}
    )

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert other_email.status_code == 200


async def test_changing_client_written_ip_does_not_evade_lock(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    body = {"email": restaurant_a.waiter.email, "password": "no-es-esta"}
    # El proxy agrega la IP real al final; lo de antes lo inventa quien llama.
    for fake_hop in range(5):
        failed = await client.post(
            url("auth/login"),
            json=body,
            headers={"x-forwarded-for": f"10.0.0.{fake_hop}, 200.48.1.7"},
        )
        assert failed.status_code == 401

    blocked = await client.post(
        url("auth/login"),
        json={**body, "password": VALID_PASSWORD},
        headers={"x-forwarded-for": "10.9.9.9, 200.48.1.7"},
    )

    assert blocked.status_code == 429


async def test_session_is_renewed_with_new_token(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    renewed = await client.post(url("auth/refresh"), headers=authorization_for(restaurant_a.waiter))
    no_token = await client.post(url("auth/refresh"))

    assert renewed.status_code == 200
    assert renewed.json()["access_token"]
    assert renewed.json()["user"]["email"] == restaurant_a.waiter.email
    assert no_token.status_code == 401


TERMS_URL = url("auth/me/terms")
ACTIVITY_URL = url("activity")


async def test_new_account_has_not_accepted_terms_and_accepting_is_logged(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    # Ley N.º 29733: sin aceptar los términos vigentes, la interfaz no deja trabajar.
    waiter_headers = authorization_for(restaurant_a.waiter)
    before = (await client.get(ME_URL, headers=waiter_headers)).json()

    accepted = await client.post(
        TERMS_URL, json={"version": before["terms_version"]}, headers=waiter_headers
    )
    after = (await client.get(ME_URL, headers=waiter_headers)).json()
    activity_log = await client.get(ACTIVITY_URL, headers=authorization_for(restaurant_a.admin))

    assert before[ActivityKind.TERMS_ACCEPTED.value] is False
    assert before["terms_version"]
    assert accepted.status_code == 200
    assert accepted.json()[ActivityKind.TERMS_ACCEPTED.value] is True
    assert after[ActivityKind.TERMS_ACCEPTED.value] is True
    assert any(
        entry["kind"] == ActivityKind.TERMS_ACCEPTED.value
        and entry["user_id"] == restaurant_a.waiter.id
        for entry in activity_log.json()["items"]
    )


async def test_accepting_outdated_version_returns_409(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    waiter_headers = authorization_for(restaurant_a.waiter)
    old = await client.post(TERMS_URL, json={"version": "2020-01"}, headers=waiter_headers)
    session_data = (await client.get(ME_URL, headers=waiter_headers)).json()

    assert old.status_code == 409
    assert "cambiaron" in old.json()["detail"]
    assert session_data[ActivityKind.TERMS_ACCEPTED.value] is False


async def test_changing_password_returns_new_token_and_cuts_the_others(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    # Dos sesiones abiertas de la misma cuenta, emitidas antes del cambio.
    current_headers = authorization_for(restaurant_a.waiter)
    old_token_headers = _token_from_an_hour_ago(restaurant_a.waiter.id or 0, restaurant_a.id)
    assert (await client.get(ME_URL, headers=old_token_headers)).status_code == 200

    response = await client.post(
        PASSWORD_URL,
        json={"current_password": VALID_PASSWORD, "new_password": "otra-contrasena-larga"},
        headers=current_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["id"] == restaurant_a.waiter.id
    # Lo viejo muere, incluido el token con el que se pidió el cambio.
    assert (await client.get(ME_URL, headers=current_headers)).status_code == 401
    assert (await client.get(ME_URL, headers=old_token_headers)).status_code == 401
    # El nuevo entra aunque se haya emitido en el mismo segundo del cambio.
    new_headers = {"Authorization": f"Bearer {body['access_token']}"}
    session_data = await client.get(ME_URL, headers=new_headers)
    assert session_data.status_code == 200
    assert session_data.json()["user"] == body["user"]


def _token_from_an_hour_ago(user_id: int, restaurant_id: int) -> dict[str, str]:
    before = datetime.now(UTC) - timedelta(hours=1)
    token = jwt.encode(
        {
            "sub": str(user_id),
            "scope": "restaurant",
            "restaurant_id": restaurant_id,
            "iat": before,
            "exp": before + timedelta(hours=2),
        },
        "secreto-de-prueba-con-largo-suficiente",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}
