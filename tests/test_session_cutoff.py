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

from resthub.core.identity import token_predates
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.exceptions import AdminUnavailableError
from resthub.modules.platform.use_cases.manage_admins import ChangeAdminStatus
from tests.conftest import (
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    build_user,
    staffed_restaurant,
)

API = "/api/v1"
ME_URL = f"{API}/auth/me"
STAFF_URL = f"{API}/staff"
PLATFORM_ME_URL = f"{API}/platform/auth/me"
SECRET = "secreto-de-prueba-con-largo-suficiente"


def _hace_una_hora(claims: dict[str, object]) -> dict[str, str]:
    """Un token con firma válida emitido antes de cualquier corte de esta prueba."""
    antes = datetime.now(UTC) - timedelta(hours=1)
    payload = {**claims, "iat": antes, "exp": antes + timedelta(hours=2)}
    return {"Authorization": f"Bearer {jwt.encode(payload, SECRET, algorithm='HS256')}"}


def _nuevo(token_value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


# --- Dominio ------------------------------------------------------------------------


def test_token_predates_compara_con_fraccion_de_segundo() -> None:
    corte = datetime.fromtimestamp(1_000, UTC)

    assert token_predates(999, corte)
    assert token_predates(999.9, corte)
    assert not token_predates(1_000.5, corte)
    assert not token_predates(1_000, corte)
    assert not token_predates(0, None)
    # SQLite devuelve la fecha sin zona: se lee como UTC, no como hora local.
    assert token_predates(999, corte.replace(tzinfo=None))
    assert not token_predates(1_000, corte.replace(tzinfo=None))


def test_desactivar_sella_el_corte_y_reactivar_lo_conserva() -> None:
    usuario = build_user(1, "ana@local.pe", Role.owner(1))
    assert usuario.sessions_valid_since is None

    usuario.deactivate()
    sellado = usuario.sessions_valid_since
    usuario.activate()

    assert sellado is not None
    assert (usuario.is_active, usuario.sessions_valid_since) == (True, sellado)


def test_desactivar_una_cuenta_de_plataforma_sella_el_corte() -> None:
    admin = PlatformAdmin(email="equipo@resthub.dev", full_name="Equipo", password_hash="x")

    admin.deactivate()
    sellado = admin.sessions_valid_since
    admin.activate()

    assert sellado is not None
    assert (admin.is_active, admin.sessions_valid_since) == (True, sellado)


# --- Personal -----------------------------------------------------------------------


async def test_desactivar_y_reactivar_al_personal_no_revive_su_token_viejo(
    client: AsyncClient, local_a: StaffedRestaurant, session: AsyncSession
) -> None:
    mesero = local_a.waiter
    viejo = _hace_una_hora(
        {"sub": str(mesero.id), "scope": "restaurant", "restaurant_id": local_a.id}
    )
    encargado = authorization_for(local_a.admin)
    assert (await client.get(ME_URL, headers=viejo)).status_code == 200

    apagar = await client.patch(
        f"{STAFF_URL}/{mesero.id}/status", json={"is_active": False}, headers=encargado
    )
    assert apagar.status_code == 200
    assert (await client.get(ME_URL, headers=viejo)).status_code == 401

    prender = await client.patch(
        f"{STAFF_URL}/{mesero.id}/status", json={"is_active": True}, headers=encargado
    )
    assert prender.status_code == 200
    # El token de antes de desactivarla sigue muerto; uno nuevo sí entra.
    assert (await client.get(ME_URL, headers=viejo)).status_code == 401
    nuevo = _nuevo(TEST_TOKEN_SERVICE.issue(mesero.id or 0, local_a.id).value)
    assert (await client.get(ME_URL, headers=nuevo)).status_code == 200


async def test_desactivar_a_uno_no_corta_las_sesiones_de_otro(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    await client.patch(
        f"{STAFF_URL}/{local_a.waiter.id}/status",
        json={"is_active": False},
        headers=authorization_for(local_a.admin),
    )

    assert (
        await client.get(
            ME_URL,
            headers=_hace_una_hora(
                {"sub": str(local_a.admin.id), "scope": "restaurant", "restaurant_id": local_a.id}
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


async def _cambiar_estado(session: AsyncSession, email: str, *, is_active: bool) -> None:
    await ChangeAdminStatus(SqlAlchemyPlatformAdminRepository(session))(email, is_active=is_active)
    await session.commit()


async def test_el_corte_de_una_cuenta_de_plataforma_se_guarda(
    session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    assert platform_admin.sessions_valid_since is None

    await _cambiar_estado(session, platform_admin.email, is_active=False)
    guardada = await SqlAlchemyPlatformAdminRepository(session).get(platform_admin.id or 0)

    assert guardada is not None
    assert guardada.is_active is False
    assert guardada.sessions_valid_since is not None


async def test_desactivar_y_reactivar_a_un_admin_de_plataforma_no_revive_su_token_viejo(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    viejo = _hace_una_hora({"sub": str(platform_admin.id), "scope": "platform"})
    assert (await client.get(PLATFORM_ME_URL, headers=viejo)).status_code == 200

    await _cambiar_estado(session, platform_admin.email, is_active=False)
    assert (await client.get(PLATFORM_ME_URL, headers=viejo)).status_code == 401

    await _cambiar_estado(session, platform_admin.email, is_active=True)
    assert (await client.get(PLATFORM_ME_URL, headers=viejo)).status_code == 401
    nuevo = _nuevo(TEST_TOKEN_SERVICE.issue_platform(platform_admin.id or 0).value)
    assert (await client.get(PLATFORM_ME_URL, headers=nuevo)).status_code == 200


async def test_cambiar_el_estado_de_un_admin_que_no_existe_falla(session: AsyncSession) -> None:
    with pytest.raises(AdminUnavailableError):
        await _cambiar_estado(session, "nadie@resthub.dev", is_active=False)


async def test_la_vista_previa_abierta_no_revive_al_reactivar_la_cuenta_de_plataforma(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    sandbox = await staffed_restaurant(session, "muestra", is_sandbox=True)
    vieja = _hace_una_hora(
        {
            "sub": str(sandbox.admin.id),
            "scope": "restaurant",
            "restaurant_id": sandbox.id,
            "preview": True,
            "platform_admin_id": platform_admin.id,
        }
    )
    assert (await client.get(ME_URL, headers=vieja)).status_code == 200

    await _cambiar_estado(session, platform_admin.email, is_active=False)
    await _cambiar_estado(session, platform_admin.email, is_active=True)

    assert (await client.get(ME_URL, headers=vieja)).status_code == 401
