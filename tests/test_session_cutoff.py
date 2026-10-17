"""Desactivar una cuenta corta sus sesiones y reactivarla no las devuelve.

Cambiar la contraseña ya sella `sessions_valid_since` (ver `test_api_auth.py`).
Aquí se prueba la desactivación, del personal y de la administración del
sistema: `is_active` se relee en cada petición, así que lo que faltaba era que
un token emitido antes no reviviera al reactivar la cuenta.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.clock import SystemClock
from resthub.core.identity import token_predates
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.exceptions import AdminUnavailableError
from resthub.modules.platform.use_cases.manage_admins import ChangeAdminStatus
from tests.conftest import (
    API_PREFIX,
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    build_user,
    staffed_restaurant,
)
from tests.fakes import FIXED_NOW

API = API_PREFIX
ME_URL = f"{API}/auth/me"
STAFF_URL = f"{API}/staff"
PLATFORM_ME_URL = f"{API}/platform/auth/me"
SECRET = "secreto-de-prueba-con-largo-suficiente"


def _issued_an_hour_ago(claims: dict[str, object]) -> dict[str, str]:
    """Un token con firma válida emitido antes de cualquier corte de esta prueba."""
    before = SystemClock().now() - timedelta(hours=1)
    payload = {**claims, "iat": before, "exp": before + timedelta(hours=2)}
    return {"Authorization": f"Bearer {jwt.encode(payload, SECRET, algorithm='HS256')}"}


def _bearer(token_value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


# --- Dominio ------------------------------------------------------------------------


def test_token_predates_compares_with_fraction_of_second() -> None:
    cutoff = datetime.fromtimestamp(1_000, UTC)

    assert token_predates(999, cutoff)
    assert token_predates(999.9, cutoff)
    assert not token_predates(1_000.5, cutoff)
    assert not token_predates(1_000, cutoff)
    assert not token_predates(0, None)
    # SQLite devuelve la fecha sin zona: se lee como UTC, no como hora local.
    assert token_predates(999, cutoff.replace(tzinfo=None))
    assert not token_predates(1_000, cutoff.replace(tzinfo=None))


def test_deactivate_seals_cutoff_and_reactivate_keeps_it() -> None:
    user = build_user(1, "ana@local.pe", Role.owner(1))
    assert user.sessions_valid_since is None

    user.deactivate(FIXED_NOW, 7, "Dejó de trabajar en el local")
    sealed = user.sessions_valid_since
    user.activate()

    assert sealed is not None
    assert (user.is_active, user.sessions_valid_since) == (True, sealed)


def test_deactivating_platform_account_seals_cutoff() -> None:
    admin = PlatformAdmin(email="equipo@resthub.dev", full_name="Equipo", password_hash="x")

    admin.deactivate(FIXED_NOW)
    sealed = admin.sessions_valid_since
    admin.activate()

    assert sealed is not None
    assert (admin.is_active, admin.sessions_valid_since) == (True, sealed)


# --- Personal -----------------------------------------------------------------------


async def test_deactivate_and_reactivate_staff_does_not_revive_old_token(
    client: AsyncClient, restaurant_a: StaffedRestaurant, session: AsyncSession
) -> None:
    waiter = restaurant_a.waiter
    old_token = _issued_an_hour_ago(
        {"sub": str(waiter.id), "scope": "restaurant", "restaurant_id": restaurant_a.id}
    )
    owner = authorization_for(restaurant_a.admin)
    assert (await client.get(ME_URL, headers=old_token)).status_code == 200

    deactivate_response = await client.patch(
        f"{STAFF_URL}/{waiter.id}/status",
        json={"is_active": False, "reason": "Terminó su contrato"},
        headers=owner,
    )
    assert deactivate_response.status_code == 200
    assert (await client.get(ME_URL, headers=old_token)).status_code == 401

    activate_response = await client.patch(
        f"{STAFF_URL}/{waiter.id}/status", json={"is_active": True}, headers=owner
    )
    assert activate_response.status_code == 200
    # El token de antes de desactivarla sigue muerto; uno nuevo sí entra.
    assert (await client.get(ME_URL, headers=old_token)).status_code == 401
    new_token = _bearer(TEST_TOKEN_SERVICE.issue(waiter.id or 0, restaurant_a.id).value)
    assert (await client.get(ME_URL, headers=new_token)).status_code == 200


async def test_deactivating_one_does_not_cut_anothers_sessions(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await client.patch(
        f"{STAFF_URL}/{restaurant_a.waiter.id}/status",
        json={"is_active": False, "reason": "Terminó su contrato"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert (
        await client.get(
            ME_URL,
            headers=_issued_an_hour_ago(
                {
                    "sub": str(restaurant_a.admin.id),
                    "scope": "restaurant",
                    "restaurant_id": restaurant_a.id,
                }
            ),
        )
    ).status_code == 200


# --- Administración del sistema -----------------------------------------------------


@pytest.fixture
async def platform_admin(session: AsyncSession) -> PlatformAdmin:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    return admin


async def _change_status(session: AsyncSession, email: str, *, is_active: bool) -> None:
    await ChangeAdminStatus(SqlAlchemyPlatformAdminRepository(session), clock=SystemClock())(
        email, is_active=is_active
    )
    await session.commit()


async def test_platform_account_cutoff_is_persisted(
    session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    assert platform_admin.sessions_valid_since is None

    await _change_status(session, platform_admin.email, is_active=False)
    saved = await SqlAlchemyPlatformAdminRepository(session).get(platform_admin.id or 0)

    assert saved is not None
    assert saved.is_active is False
    assert saved.sessions_valid_since is not None


async def test_deactivate_and_reactivate_platform_admin_does_not_revive_old_token(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    old_token = _issued_an_hour_ago({"sub": str(platform_admin.id), "scope": "platform"})
    assert (await client.get(PLATFORM_ME_URL, headers=old_token)).status_code == 200

    await _change_status(session, platform_admin.email, is_active=False)
    assert (await client.get(PLATFORM_ME_URL, headers=old_token)).status_code == 401

    await _change_status(session, platform_admin.email, is_active=True)
    assert (await client.get(PLATFORM_ME_URL, headers=old_token)).status_code == 401
    new_token = _bearer(TEST_TOKEN_SERVICE.issue_platform(platform_admin.id or 0).value)
    assert (await client.get(PLATFORM_ME_URL, headers=new_token)).status_code == 200


async def test_changing_status_of_nonexistent_admin_fails(session: AsyncSession) -> None:
    with pytest.raises(AdminUnavailableError):
        await _change_status(session, "nadie@resthub.dev", is_active=False)


async def test_open_preview_does_not_revive_on_platform_account_reactivation(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    sandbox = await staffed_restaurant(session, "muestra", is_sandbox=True)
    old_token = _issued_an_hour_ago(
        {
            "sub": str(sandbox.admin.id),
            "scope": "restaurant",
            "restaurant_id": sandbox.id,
            "preview": True,
            "platform_admin_id": platform_admin.id,
        }
    )
    assert (await client.get(ME_URL, headers=old_token)).status_code == 200

    await _change_status(session, platform_admin.email, is_active=False)
    await _change_status(session, platform_admin.email, is_active=True)

    assert (await client.get(ME_URL, headers=old_token)).status_code == 401
