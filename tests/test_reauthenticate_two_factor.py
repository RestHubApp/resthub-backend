"""Confirmar la contraseña pide también el segundo factor si la cuenta lo tiene (ASVS V7.5.1)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.two_factor import current_step, totp
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
    url,
)

LOGIN_URL = url("auth/login")
REAUTHENTICATE_URL = url("auth/me/reauthenticate")
TWO_FACTOR_URL = url("auth/me/two-factor")
SESSIONS_URL = url("auth/me/sessions")
ME_URL = url("auth/me")
OWNER_EMAIL = "encargado@local-a.pe"
ADMIN_EMAIL = "plataforma@resthub.dev"
ANDROID_CHROME = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/129.0 Mobile Safari"


def _code(secret: str, clock: FakeClock) -> str:
    return totp(secret, current_step(clock.now()))


async def _login(
    client: AsyncClient, email: str, otp_code: str | None = None, agent: str = "pruebas"
) -> dict[str, str]:
    body: dict[str, str] = {"email": email, "password": VALID_PASSWORD}
    if otp_code is not None:
        body["otp_code"] = otp_code
    response = await client.post(LOGIN_URL, json=body, headers={"User-Agent": agent})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _reauthenticate(
    client: AsyncClient, headers: dict[str, str], **extra: str
) -> tuple[int, dict[str, object]]:
    response = await client.post(
        REAUTHENTICATE_URL, json={"password": VALID_PASSWORD, **extra}, headers=headers
    )
    return response.status_code, response.json()


async def _enable(
    client: AsyncClient, clock: FakeClock, headers: dict[str, str]
) -> tuple[str, list[str]]:
    """Activa la verificación con la sesión `headers`, ya reautenticada."""
    setup = await client.post(f"{TWO_FACTOR_URL}/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    confirmed = await client.post(
        f"{TWO_FACTOR_URL}/confirm", json={"code": _code(secret, clock)}, headers=headers
    )
    assert confirmed.status_code == 200, confirmed.text
    # Un código ya aceptado no vuelve a servir: el próximo es del paso siguiente.
    clock.advance(30)
    return secret, confirmed.json()["recovery_codes"]


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


async def test_reauthenticate_without_two_factor_needs_only_the_password(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = await _login(client, OWNER_EMAIL)

    status, _ = await _reauthenticate(client, headers)

    assert status == 200


async def test_reauthenticate_with_two_factor_asks_for_the_code(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    secret, _ = await _enable(client, clock, owner)
    headers = await _login(client, OWNER_EMAIL, _code(secret, clock))
    clock.advance(30)

    missing_status, missing = await _reauthenticate(client, headers)
    wrong_status, wrong = await _reauthenticate(client, headers, otp_code="000000")
    right_status, right = await _reauthenticate(client, headers, otp_code=_code(secret, clock))

    assert (missing_status, missing["code"]) == (401, "two_factor_required")
    assert (wrong_status, wrong["code"]) == (401, "invalid_two_factor_code")
    assert right_status == 200, right
    # El 401 del segundo paso no cierra la sesión: es la credencial que se confirma.
    assert (await client.get(ME_URL, headers=headers)).status_code == 200


async def test_reauthenticate_with_two_factor_rejects_a_wrong_password_first(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    secret, _ = await _enable(client, clock, owner)

    response = await client.post(
        REAUTHENTICATE_URL,
        json={"password": "otra", "otp_code": _code(secret, clock)},
        headers=owner,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "wrong_current_password"


async def test_reauthenticate_accepts_a_recovery_code_once(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    _, recovery = await _enable(client, clock, owner)

    first_status, _ = await _reauthenticate(client, owner, otp_code=recovery[0])
    again_status, again = await _reauthenticate(client, owner, otp_code=recovery[0])

    assert first_status == 200
    assert (again_status, again["code"]) == (401, "invalid_two_factor_code")
    activity = await client.get(url("activity"), headers=owner)
    kinds = {item["kind"] for item in activity.json()["items"]}
    assert ActivityKind.RECOVERY_CODE_USED.value in kinds


async def test_reauthenticate_accepts_a_recovery_code_typed_with_spaces(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    _, recovery = await _enable(client, clock, owner)
    # Como en el acceso: en minúsculas y con espacios entre los grupos pasa de 32.
    typed = " - ".join(recovery[0].lower().split("-"))
    assert len(typed) > 32

    status, body = await _reauthenticate(client, owner, otp_code=typed)

    assert status == 200, body


async def test_wrong_codes_count_as_failed_reauthentication_attempts(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    owner = authorization_for(restaurant_a.admin)
    secret, _ = await _enable(client, clock, owner)

    statuses = [(await _reauthenticate(client, owner, otp_code="000000"))[0] for _ in range(5)]
    blocked, body = await _reauthenticate(client, owner, otp_code=_code(secret, clock))

    assert statuses == [401] * 5
    assert (blocked, body["code"]) == (429, "login_throttled")


async def test_platform_reauthenticate_with_two_factor_asks_for_the_code(
    client: AsyncClient, clock: FakeClock, platform_admin: PlatformAdmin
) -> None:
    headers = await _login(client, ADMIN_EMAIL)
    assert (await _reauthenticate(client, headers))[0] == 200
    secret, _ = await _enable(client, clock, headers)

    missing_status, missing = await _reauthenticate(client, headers)
    right_status, _ = await _reauthenticate(client, headers, otp_code=_code(secret, clock))

    assert (missing_status, missing["code"]) == (401, "two_factor_required")
    assert right_status == 200
