"""Cuándo se cierran las sesiones (ASVS V7.4.1, V7.4.3, V7.4.5, V7.1.2)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from scripts.revoke_all_sessions import UnknownRestaurantError, revoke_sessions
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.access_history import PLATFORM_ACCOUNT, STAFF_ACCOUNT
from resthub.core.account_sessions import SessionOrigin, SqlSessionRegistry
from resthub.core.clock import SystemClock
from resthub.core.config import Settings
from resthub.core.geo import NoGeoLocator
from resthub.core.tokens import JwtTokenService
from resthub.core.two_factor import current_step, totp
from resthub.modules.accounts.adapters.persistence.directories import SqlAlchemyRestaurantDirectory
from resthub.modules.accounts.adapters.persistence.sqlalchemy_preview_codes import (
    SqlAlchemyPreviewCodeRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.use_cases.preview import IssuePreviewCode, IssuePreviewCodeCommand
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import (
    TEST_HASHER,
    VALID_PASSWORD,
    FakeClock,
    StaffedRestaurant,
    authorization_for,
    staffed_restaurant,
    url,
)
from tests.fakes import FixedClock

LOGIN_URL = url("auth/login")
LOGOUT_URL = url("auth/logout")
PREVIEW_URL = url("auth/preview")
ME_URL = url("auth/me")
REAUTHENTICATE_URL = url("auth/me/reauthenticate")
TWO_FACTOR_URL = url("auth/me/two-factor")
OWNER_EMAIL = "encargado@local-a.pe"


@pytest.fixture
async def sandbox(session: AsyncSession) -> StaffedRestaurant:
    return await staffed_restaurant(session, "muestra", is_sandbox=True)


@pytest.fixture
async def admin_id(session: AsyncSession) -> int:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    return admin.id or 0


async def _login(client: AsyncClient, email: str, agent: str) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL,
        json={"email": email, "password": VALID_PASSWORD},
        headers={"User-Agent": agent},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _status(client: AsyncClient, headers: dict[str, str]) -> int:
    return (await client.get(ME_URL, headers=headers)).status_code


def _registry(clock: FixedClock, max_sessions: int = 10) -> SqlSessionRegistry:
    return SqlSessionRegistry(
        clock=clock,
        geo=NoGeoLocator(),
        token_ttl=timedelta(hours=1),
        max_sessions=max_sessions,
    )


async def _open(
    session: AsyncSession,
    registry: SqlSessionRegistry,
    session_id: str,
    *,
    account: tuple[str, int, int | None] = (STAFF_ACCOUNT, 7, 1),
) -> None:
    kind, account_id, restaurant_id = account
    await registry.open(
        session,
        session_id=session_id,
        account_kind=kind,
        account_id=account_id,
        restaurant_id=restaurant_id,
        origin=SessionOrigin("200.48.1.10", ""),
    )


# --- Vista previa (V7.4.1) --------------------------------------------------------


async def test_logging_out_of_a_preview_invalidates_its_token(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    issued = await IssuePreviewCode(
        SqlAlchemyPreviewCodeRepository(session),
        SqlAlchemyUserRepository(session),
        SqlAlchemyRestaurantDirectory(session),
        SystemClock(),
    )(IssuePreviewCodeCommand(user_id=sandbox.waiter.id or 0, platform_admin_id=admin_id))
    await session.commit()
    exchanged = await client.post(PREVIEW_URL, json={"code": issued.code})
    assert exchanged.status_code == 200, exchanged.text
    headers = {"Authorization": f"Bearer {exchanged.json()['access_token']}"}
    assert await _status(client, headers) == 200

    assert (await client.post(LOGOUT_URL, headers=headers)).status_code == 204

    response = await client.get(ME_URL, headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


async def test_a_preview_token_without_its_session_is_rejected(
    client: AsyncClient, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    # Firmado como en producción, sin la sesión que abre el canje.
    tokens = JwtTokenService(
        secret_key="secreto-de-prueba-con-largo-suficiente", algorithm="HS256", ttl_seconds=60
    )
    token = tokens.issue_preview(sandbox.waiter.id or 0, sandbox.id, admin_id)

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {token.value}"})

    assert response.status_code == 401


# --- Verificación en dos pasos (V7.4.3) -----------------------------------------------


async def _reauthenticate(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post(
        REAUTHENTICATE_URL, json={"password": VALID_PASSWORD}, headers=headers
    )
    assert response.status_code == 200, response.text


async def test_enabling_and_disabling_two_factor_closes_the_other_sessions(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    phone = await _login(client, OWNER_EMAIL, "celular")
    laptop = await _login(client, OWNER_EMAIL, "laptop")
    await _reauthenticate(client, laptop)
    setup = await client.post(f"{TWO_FACTOR_URL}/setup", headers=laptop)
    secret = setup.json()["secret"]

    confirmed = await client.post(
        f"{TWO_FACTOR_URL}/confirm",
        json={"code": totp(secret, current_step(clock.now()))},
        headers=laptop,
    )

    assert confirmed.status_code == 200, confirmed.text
    assert (await _status(client, phone), await _status(client, laptop)) == (401, 200)

    clock.advance(30)
    tablet_response = await client.post(
        LOGIN_URL,
        json={
            "email": OWNER_EMAIL,
            "password": VALID_PASSWORD,
            "otp_code": totp(secret, current_step(clock.now())),
        },
        headers={"User-Agent": "tableta"},
    )
    tablet = {"Authorization": f"Bearer {tablet_response.json()['access_token']}"}
    assert (await client.delete(TWO_FACTOR_URL, headers=laptop)).status_code == 204

    assert (await _status(client, tablet), await _status(client, laptop)) == (401, 200)


# --- Cierre de todas las sesiones de un local (V7.4.5) ----------------------------------


async def test_revoke_sessions_closes_only_that_restaurant(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    owner_a = authorization_for(restaurant_a.admin)
    waiter_a = authorization_for(restaurant_a.waiter)
    owner_b = authorization_for(restaurant_b.admin)
    for headers in (owner_a, waiter_a, owner_b):
        assert await _status(client, headers) == 200

    closed = await revoke_sessions(session, SystemClock(), slug=restaurant_a.restaurant.slug)

    assert closed == 2
    assert (await _status(client, owner_a), await _status(client, waiter_a)) == (401, 401)
    assert await _status(client, owner_b) == 200


async def test_revoke_sessions_of_everyone(
    session: AsyncSession,
) -> None:
    clock = FixedClock()
    registry = _registry(clock)
    await _open(session, registry, "del-local")
    await _open(session, registry, "de-plataforma", account=(PLATFORM_ACCOUNT, 1, None))

    closed = await revoke_sessions(session, clock, slug=None)

    assert closed == 2
    assert await registry.list_open(session, STAFF_ACCOUNT, 7) == []
    assert await registry.list_open(session, PLATFORM_ACCOUNT, 1) == []


async def test_revoke_sessions_of_an_unknown_restaurant_fails(session: AsyncSession) -> None:
    with pytest.raises(UnknownRestaurantError):
        await revoke_sessions(session, SystemClock(), slug="no-existe")


# --- Tope de sesiones abiertas (V7.1.2) ---------------------------------------------------


def test_settings_max_sessions_per_account_defaults_to_ten() -> None:
    assert Settings().max_sessions_per_account == 10


async def test_opening_one_session_too_many_closes_the_least_recently_used(
    session: AsyncSession,
) -> None:
    clock = FixedClock()
    registry = _registry(clock, max_sessions=3)
    for name in ("primera", "segunda", "tercera"):
        await _open(session, registry, name)
        clock.advance(minutes=1)
    first = await registry.find(session, "primera")
    assert first is not None
    # La primera se usó hace poco: la menos usada ahora es la segunda.
    await registry.touch(session, first, force=True)
    clock.advance(minutes=1)

    await _open(session, registry, "cuarta")
    await _open(session, registry, "otra-cuenta", account=(STAFF_ACCOUNT, 8, 1))

    opened = {state.id for state in await registry.list_open(session, STAFF_ACCOUNT, 7)}
    assert opened == {"cuarta", "primera", "tercera"}
    assert len(await registry.list_open(session, STAFF_ACCOUNT, 8)) == 1
