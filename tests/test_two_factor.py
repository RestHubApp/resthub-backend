"""Verificación en dos pasos con TOTP (RFS-22)."""

from __future__ import annotations

import base64
import hashlib
import math
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.two_factor import (
    SecondFactor,
    SqlTwoFactorStore,
    TwoFactorCredentialRow,
    current_step,
    hash_recovery,
    is_outdated_recovery_hash,
    locked_credential,
    matching_step,
    new_recovery_code,
    normalize_recovery,
    recovery_hash_matches,
    totp,
)
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
from tests.fakes import FixedClock

LOGIN_URL = url("auth/login")
TWO_FACTOR_URL = url("auth/me/two-factor")
OWNER_EMAIL = "encargado@local-a.pe"
ADMIN_EMAIL = "plataforma@resthub.dev"
# RFC 6238, apéndice B: la clave ASCII «12345678901234567890», en base32.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(
    ("moment", "code"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_codes_follow_rfc_6238(moment: int, code: str) -> None:
    step = current_step(datetime.fromtimestamp(moment, tz=UTC))

    assert totp(RFC_SECRET, step) == code


def test_a_code_tolerates_one_step_and_is_not_reused() -> None:
    now = datetime.fromtimestamp(1111111109, tz=UTC)
    step = current_step(now)
    previous = totp(RFC_SECRET, step - 1)

    assert matching_step(RFC_SECRET, previous, now) == step - 1
    assert matching_step(RFC_SECRET, totp(RFC_SECRET, step - 2), now) is None
    assert matching_step(RFC_SECRET, previous, now, after=step - 1) is None
    assert matching_step(RFC_SECRET, "12ab56", now) is None


def test_recovery_codes_ignore_dashes_and_case() -> None:
    assert hash_recovery("ABCD-EFGH-JK") == hash_recovery("abcdefghjk")
    assert hash_recovery("abcd efgh\u2013jk") == hash_recovery(" ABCDEFGHJK ")
    assert normalize_recovery("K7QM - 4XWP\t9RTA") == "k7qm4xwp9rta"


def test_new_recovery_codes_have_at_least_128_bits() -> None:
    code = new_recovery_code()
    groups = code.split("-")
    plain = normalize_recovery(code)
    alphabet = set("abcdefghjkmnpqrstuvwxyz23456789")

    assert [len(group) for group in groups] == [4] * 7
    assert code == code.upper()
    assert set(plain) <= alphabet
    # 28 caracteres de un alfabeto de 31: 28 * log2(31) ≈ 138,7 bits (ASVS 11.5.1).
    assert len(plain) * math.log2(len(alphabet)) >= 128
    assert len({new_recovery_code() for _ in range(50)}) == 50


def test_old_short_recovery_hashes_still_match_and_are_flagged() -> None:
    legacy = hashlib.sha256(b"abcdefghjk").hexdigest()
    previous = "v2:" + hashlib.sha256(b"abcdefghjkmnpqrstuvwxyz2").hexdigest()
    fresh = hash_recovery("ABCD-EFGH-JKMN-PQRS-TUVW-XYZ2-3456")

    assert recovery_hash_matches("abcde-fghjk", legacy)
    assert recovery_hash_matches("ABCD-EFGH-JKMN-PQRS-TUVW-XYZ2", previous)
    assert recovery_hash_matches("abcd efgh jkmn pqrs tuvw xyz2 3456", fresh)
    assert not recovery_hash_matches("abcdefghjm", legacy)
    assert is_outdated_recovery_hash(legacy)
    assert is_outdated_recovery_hash(previous)
    assert not is_outdated_recovery_hash(fresh)


async def _login(client: AsyncClient, email: str = OWNER_EMAIL, **extra: str) -> Any:
    return await client.post(LOGIN_URL, json={"email": email, "password": VALID_PASSWORD, **extra})


async def _headers(client: AsyncClient, email: str = OWNER_EMAIL) -> dict[str, str]:
    response = await _login(client, email)
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    confirmed = await client.post(
        url("auth/me/reauthenticate"), json={"password": VALID_PASSWORD}, headers=headers
    )
    assert confirmed.status_code == 200
    return headers


def _code(secret: str, clock: FakeClock) -> str:
    return totp(secret, current_step(clock.now()))


async def _enable(
    client: AsyncClient, clock: FakeClock, email: str = OWNER_EMAIL
) -> tuple[str, list[str]]:
    headers = await _headers(client, email)
    setup = await client.post(f"{TWO_FACTOR_URL}/setup", headers=headers)
    secret = setup.json()["secret"]
    confirmed = await client.post(
        f"{TWO_FACTOR_URL}/confirm", json={"code": _code(secret, clock)}, headers=headers
    )
    assert confirmed.status_code == 200, confirmed.text
    # Un código ya aceptado no vuelve a servir: el próximo acceso es en el paso siguiente.
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


async def test_enabling_it_needs_the_password_and_a_first_code(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    login = await _login(client)
    plain = {"Authorization": f"Bearer {login.json()['access_token']}"}
    blocked = await client.post(f"{TWO_FACTOR_URL}/setup", headers=plain)
    headers = await _headers(client)

    setup = await client.post(f"{TWO_FACTOR_URL}/setup", headers=headers)
    pending = await client.get(TWO_FACTOR_URL, headers=headers)
    wrong = await client.post(f"{TWO_FACTOR_URL}/confirm", json={"code": "000000"}, headers=headers)
    confirmed = await client.post(
        f"{TWO_FACTOR_URL}/confirm",
        json={"code": _code(setup.json()["secret"], clock)},
        headers=headers,
    )
    enabled = await client.get(TWO_FACTOR_URL, headers=headers)

    assert blocked.json()["code"] == "reauthentication_required"
    assert setup.json()["otpauth_uri"].startswith(
        "otpauth://totp/RestHub%3Aencargado%40local-a.pe?secret="
    )
    assert pending.json() == {
        "enabled": False,
        "pending": True,
        "recovery_codes_left": 0,
        "recovery_codes_outdated": False,
    }
    assert (wrong.status_code, wrong.json()["code"]) == (400, "invalid_two_factor_code")
    assert len(confirmed.json()["recovery_codes"]) == 10
    assert all(len(code) == 34 for code in confirmed.json()["recovery_codes"])
    assert enabled.json() == {
        "enabled": True,
        "pending": False,
        "recovery_codes_left": 10,
        "recovery_codes_outdated": False,
    }


async def test_the_login_asks_for_the_code(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    secret, _ = await _enable(client, clock)

    missing = await _login(client)
    wrong = await _login(client, otp_code="000000")
    code = _code(secret, clock)
    right = await _login(client, otp_code=code)
    replayed = await _login(client, otp_code=code)

    assert (missing.status_code, missing.json()["code"]) == (401, "two_factor_required")
    assert (wrong.status_code, wrong.json()["code"]) == (401, "invalid_two_factor_code")
    assert right.status_code == 200
    assert replayed.json()["code"] == "invalid_two_factor_code"


async def test_a_recovery_code_works_once(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    _, codes = await _enable(client, clock)

    first = await _login(client, otp_code=codes[0].lower().replace("-", " "))
    again = await _login(client, otp_code=codes[0])

    assert first.status_code == 200
    assert again.status_code == 401
    headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    status = await client.get(TWO_FACTOR_URL, headers=headers)
    assert status.json()["recovery_codes_left"] == 9
    activity = await client.get(url("activity"), headers=authorization_for(restaurant_a.admin))
    kinds = {item["kind"] for item in activity.json()["items"]}
    assert {ActivityKind.TWO_FACTOR_ENABLED.value, ActivityKind.RECOVERY_CODE_USED.value} <= kinds


async def test_renewing_the_recovery_codes_drops_the_old_ones(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    _, old = await _enable(client, clock)
    headers = authorization_for(restaurant_a.admin)

    renewed = await client.post(f"{TWO_FACTOR_URL}/recovery-codes", headers=headers)

    assert len(renewed.json()["recovery_codes"]) == 10
    assert (await _login(client, otp_code=old[0])).status_code == 401
    assert (await _login(client, otp_code=renewed.json()["recovery_codes"][0])).status_code == 200


async def test_disabling_it(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    await _enable(client, clock)
    headers = authorization_for(restaurant_a.admin)

    assert (await client.delete(TWO_FACTOR_URL, headers=headers)).status_code == 204
    assert (await _login(client)).status_code == 200


async def test_the_states_that_do_not_apply_are_409(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)
    no_setup = await client.post(
        f"{TWO_FACTOR_URL}/confirm", json={"code": "123456"}, headers=headers
    )
    no_codes = await client.post(f"{TWO_FACTOR_URL}/recovery-codes", headers=headers)
    await _enable(client, clock)
    twice = await client.post(f"{TWO_FACTOR_URL}/setup", headers=headers)

    for response in (no_setup, no_codes, twice):
        assert (response.status_code, response.json()["code"]) == (
            409,
            "two_factor_state_conflict",
        )


async def test_a_platform_account_uses_it_in_both_logins(
    client: AsyncClient, clock: FakeClock, platform_admin: PlatformAdmin
) -> None:
    secret, _ = await _enable(client, clock, ADMIN_EMAIL)

    unified = await _login(client, ADMIN_EMAIL)
    old_route = await client.post(
        url("platform/auth/login"), json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD}
    )
    with_code = await client.post(
        url("platform/auth/login"),
        json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD, "otp_code": _code(secret, clock)},
    )

    assert unified.json()["code"] == "two_factor_required"
    assert old_route.json()["code"] == "two_factor_required"
    assert with_code.status_code == 200


async def test_a_short_recovery_code_from_before_keeps_working_until_renewed(
    client: AsyncClient, clock: FakeClock, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    await _enable(client, clock)
    row = (await session.execute(select(TwoFactorCredentialRow))).scalar_one()
    # Una fila guardada antes del cambio: hashes sin prefijo de códigos de diez caracteres.
    row.recovery_code_hashes = [hashlib.sha256(b"abcdefghjk").hexdigest()]
    await session.commit()
    headers = authorization_for(restaurant_a.admin)

    outdated = await client.get(TWO_FACTOR_URL, headers=headers)
    signed_in = await _login(client, otp_code="ABCDE-FGHJK")

    assert outdated.json()["recovery_codes_outdated"] is True
    assert signed_in.status_code == 200, signed_in.text
    renewed = await client.post(f"{TWO_FACTOR_URL}/recovery-codes", headers=headers)
    assert renewed.status_code == 200
    after = await client.get(TWO_FACTOR_URL, headers=headers)
    assert after.json()["recovery_codes_outdated"] is False


async def _store_with_two_factor(session: AsyncSession) -> tuple[SqlTwoFactorStore, str, list[str]]:
    store = SqlTwoFactorStore(clock=FixedClock())
    secret = await store.begin(session, "staff", 7)
    codes = await store.confirm(session, "staff", 7, totp(secret, current_step(store.clock.now())))
    assert codes is not None
    await session.commit()
    return store, secret, codes


def _race(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, competitor: Any) -> list[bool]:
    """Corre `competitor` tras la lectura bloqueada, como otro acceso que se adelanta."""
    original = session.execute
    raced: list[bool] = []

    async def racing(statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(statement, *args, **kwargs)
        if getattr(statement, "_for_update_arg", None) is not None and not raced:
            raced.append(True)
            await original(competitor)
        return result

    monkeypatch.setattr(session, "execute", racing)
    return raced


def test_the_credential_is_read_for_update() -> None:
    statement = locked_credential("staff", 7)

    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))


async def test_a_totp_spent_by_a_concurrent_login_does_not_work_twice(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, secret, _ = await _store_with_two_factor(session)
    assert isinstance(store.clock, FixedClock)
    store.clock.advance(seconds=30)
    step = current_step(store.clock.now())
    # El otro acceso ya aceptó este paso entre la lectura y la escritura de este.
    raced = _race(session, monkeypatch, update(TwoFactorCredentialRow).values(last_step=step))

    assert await store.verify(session, "staff", 7, totp(secret, step)) is None
    assert raced == [True]


async def test_a_recovery_code_spent_by_a_concurrent_login_does_not_work_twice(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, codes = await _store_with_two_factor(session)
    left = [hash_recovery(code) for code in codes[1:]]
    raced = _race(
        session, monkeypatch, update(TwoFactorCredentialRow).values(recovery_code_hashes=left)
    )

    assert await store.verify(session, "staff", 7, codes[0]) is None
    assert raced == [True]
    assert (await store.status(session, "staff", 7)).recovery_codes_left == 9


async def test_spending_a_code_updates_the_loaded_row(session: AsyncSession) -> None:
    store, _, codes = await _store_with_two_factor(session)
    assert (await store.status(session, "staff", 7)).recovery_codes_left == 10

    assert await store.verify(session, "staff", 7, codes[0]) is SecondFactor.RECOVERY
    assert await store.verify(session, "staff", 7, codes[0]) is None
    assert (await store.status(session, "staff", 7)).recovery_codes_left == 9
