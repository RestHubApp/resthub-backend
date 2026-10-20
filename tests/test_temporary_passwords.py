"""Contraseñas temporales que vencen y cambio de contraseña de la plataforma (ASVS 6.2.2, 6.4.1)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.clock import utc_now
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import TemporaryPasswordExpiredError
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.accounts.use_cases.change_own_password import (
    ChangeOwnPassword,
    ChangeOwnPasswordCommand,
)
from resthub.modules.platform.adapters.persistence.models import PlatformActivityRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformActivityKind, PlatformAdmin
from resthub.modules.platform.use_cases.manage_admins import RegisterAdmin, RegisterAdminCommand
from tests.conftest import (
    TEST_HASHER,
    FakeClock,
    RecordingActivity,
    StaffedRestaurant,
    authorization_for,
    url,
)
from tests.fakes import (
    FIXED_NOW,
    FakeHasher,
    FixedClock,
    InMemoryRestaurantDirectory,
    InMemoryUserRepository,
)

LOGIN_URL = url("auth/login")
OLD_PLATFORM_LOGIN_URL = url("platform/auth/login")
PASSWORD_URL = url("auth/me/password")
ADMIN_EMAIL = "lucia.rojas@resthub.dev"
TEMPORARY = "cielo-gris-sobre-lima"
LIFETIME_SECONDS = 72 * 3600


async def _admin(session: AsyncSession, *, must_change: bool, age: timedelta) -> PlatformAdmin:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email=ADMIN_EMAIL,
            full_name="Lucía Rojas",
            password_hash=TEST_HASHER.hash(TEMPORARY),
            must_change_password=must_change,
            created_at=utc_now() - age,
        )
    )
    await session.commit()
    return admin


def _bearer(response_body: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {response_body['access_token']}"}


async def test_a_platform_account_with_a_temporary_password_must_change_it(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _admin(session, must_change=True, age=timedelta(hours=1))
    login = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": TEMPORARY})
    headers = _bearer(login.json())

    blocked = await client.get(url("platform/restaurants"), headers=headers)
    wrong = await client.post(
        PASSWORD_URL,
        json={"current_password": "otra-que-no-es", "new_password": "pan-con-palta-2026"},
        headers=headers,
    )
    weak = await client.post(
        PASSWORD_URL,
        json={"current_password": TEMPORARY, "new_password": "Lucia-del-equipo-26"},
        headers=headers,
    )
    changed = await client.post(
        PASSWORD_URL,
        json={"current_password": TEMPORARY, "new_password": "pan-con-palta-2026"},
        headers=headers,
    )

    assert login.status_code == 200, login.text
    assert login.json()["kind"] == "platform"
    assert login.json()["must_change_password"] is True
    assert (blocked.status_code, blocked.json()["code"]) == (403, "password_change_required")
    assert (wrong.status_code, wrong.json()["code"]) == (400, "wrong_current_password")
    assert (weak.status_code, weak.json()["code"]) == (422, "weak_password")
    assert changed.status_code == 200, changed.text
    assert changed.json()["kind"] == "platform"
    assert changed.json()["must_change_password"] is False
    allowed = await client.get(url("platform/restaurants"), headers=_bearer(changed.json()))
    assert allowed.status_code == 200
    # El token anterior quedó cortado con el cambio.
    assert (await client.get(url("auth/me"), headers=headers)).status_code == 401
    kinds = (await session.execute(select(PlatformActivityRow.kind))).scalars().all()
    assert PlatformActivityKind.PASSWORD_CHANGED.value in kinds


async def test_a_platform_account_changes_its_own_password(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _admin(session, must_change=False, age=timedelta(days=400))
    login = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": TEMPORARY})

    changed = await client.post(
        PASSWORD_URL,
        json={"current_password": TEMPORARY, "new_password": "ñandú-🍋-" * 8},
        headers=_bearer(login.json()),
    )
    old = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": TEMPORARY})
    new = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": "ñandú-🍋-" * 8})

    assert changed.status_code == 200, changed.text
    assert (old.status_code, new.status_code) == (401, 200)


@pytest.mark.parametrize("login_url", [LOGIN_URL, OLD_PLATFORM_LOGIN_URL])
async def test_an_expired_platform_temporary_password_does_not_sign_in(
    client: AsyncClient, session: AsyncSession, login_url: str
) -> None:
    await _admin(session, must_change=True, age=timedelta(hours=73))

    response = await client.post(login_url, json={"email": ADMIN_EMAIL, "password": TEMPORARY})

    assert (response.status_code, response.json()["code"]) == (401, "temporary_password_expired")


async def test_a_staff_temporary_password_expires(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    reset = await client.post(
        url(f"staff/{restaurant_a.waiter.id}/password"),
        json={"new_password": TEMPORARY},
        headers=authorization_for(restaurant_a.admin),
    )
    body = {"email": restaurant_a.waiter.email, "password": TEMPORARY}
    assert reset.status_code == 204, reset.text
    early = await client.post(LOGIN_URL, json=body)

    clock.advance(LIFETIME_SECONDS)
    late = await client.post(LOGIN_URL, json=body)

    assert early.status_code == 200
    assert early.json()["must_change_password"] is True
    assert (late.status_code, late.json()["code"]) == (401, "temporary_password_expired")


async def test_an_expired_temporary_password_cannot_choose_the_own_one() -> None:
    users = InMemoryUserRepository()
    user = await users.add(
        User(
            restaurant_id=1,
            email="ana@example.com",
            full_name="Ana Quispe",
            role=replace(Role.waiter(1), id=2),
            password_hash=FakeHasher().hash(TEMPORARY),
        )
    )
    user.set_temporary_password(FakeHasher().hash(TEMPORARY), FIXED_NOW)
    await users.save(user)
    change = ChangeOwnPassword(
        users,
        FakeHasher(),
        RecordingActivity(),
        FixedClock(FIXED_NOW + timedelta(hours=72)),
        restaurants=InMemoryRestaurantDirectory(),
        temporary_password_lifetime=timedelta(hours=72),
    )

    with pytest.raises(TemporaryPasswordExpiredError):
        await change(
            ChangeOwnPasswordCommand(
                user_id=user.id or 0,
                current_password=TEMPORARY,
                new_password="pan-con-palta-2026",
            )
        )


async def test_staff_own_password_clears_the_temporary_mark(
    client: AsyncClient, clock: FakeClock, restaurant_a: StaffedRestaurant
) -> None:
    await client.post(
        url(f"staff/{restaurant_a.waiter.id}/password"),
        json={"new_password": TEMPORARY},
        headers=authorization_for(restaurant_a.admin),
    )
    login = await client.post(
        LOGIN_URL, json={"email": restaurant_a.waiter.email, "password": TEMPORARY}
    )
    changed = await client.post(
        PASSWORD_URL,
        json={"current_password": TEMPORARY, "new_password": "pan-con-palta-2026"},
        headers=_bearer(login.json()),
    )

    clock.advance(LIFETIME_SECONDS * 2)
    later = await client.post(
        LOGIN_URL, json={"email": restaurant_a.waiter.email, "password": "pan-con-palta-2026"}
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["kind"] == "staff"
    assert later.status_code == 200, later.text


async def test_register_admin_marks_a_temporary_password(session: AsyncSession) -> None:
    class NoStaffEmails:
        async def exists(self, email: str) -> bool:
            return False

    admin = await RegisterAdmin(
        SqlAlchemyPlatformAdminRepository(session), FakeHasher(), NoStaffEmails()
    )(
        RegisterAdminCommand(
            email=ADMIN_EMAIL, full_name="Lucía Rojas", password=TEMPORARY, temporary=True
        )
    )

    assert admin.must_change_password
    assert not admin.temporary_password_expired(admin.created_at, timedelta(hours=72))
    assert admin.temporary_password_expired(
        admin.created_at + timedelta(hours=72), timedelta(hours=72)
    )
