"""Argon2id para las contraseñas, y los hashes bcrypt de antes migran al iniciar sesión."""

from __future__ import annotations

import time

from httpx import AsyncClient
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.config import Settings
from resthub.core.security import Argon2PasswordHasher, HashCost, hasher_from_settings
from resthub.modules.accounts.adapters.persistence.models import UserRow
from resthub.modules.platform.adapters.persistence.models import PlatformAdminRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import TEST_HASHER, VALID_PASSWORD, StaffedRestaurant, authorization_for, url

LEGACY_BCRYPT = PasswordHash((BcryptHasher(rounds=4),))
ARGON2ID = "$argon2id$"


def test_new_hashes_are_argon2id_with_a_random_salt() -> None:
    first, second = TEST_HASHER.hash(VALID_PASSWORD), TEST_HASHER.hash(VALID_PASSWORD)

    assert first.startswith(ARGON2ID)
    assert first != second


def test_a_current_hash_verifies_without_asking_for_an_update() -> None:
    stored = TEST_HASHER.hash(VALID_PASSWORD)

    assert TEST_HASHER.verify_and_update(VALID_PASSWORD, stored) == (True, None)
    assert TEST_HASHER.verify_and_update("otra-contrasena", stored) == (False, None)


def test_an_old_bcrypt_hash_still_verifies_and_asks_for_argon2id() -> None:
    legacy = LEGACY_BCRYPT.hash(VALID_PASSWORD)
    assert legacy.startswith("$2")

    matches, upgraded = TEST_HASHER.verify_and_update(VALID_PASSWORD, legacy)

    assert matches
    assert upgraded is not None
    assert upgraded.startswith(ARGON2ID)
    assert TEST_HASHER.verify_and_update(VALID_PASSWORD, upgraded) == (True, None)


def test_a_wrong_password_against_bcrypt_never_offers_a_new_hash() -> None:
    legacy = LEGACY_BCRYPT.hash(VALID_PASSWORD)

    assert TEST_HASHER.verify_and_update("otra-contrasena", legacy) == (False, None)


def test_an_argon2id_hash_with_a_lower_cost_is_upgraded() -> None:
    weaker = Argon2PasswordHasher(HashCost(time_cost=1, memory_cost_kib=8, parallelism=1))
    stronger = Argon2PasswordHasher(HashCost(time_cost=2, memory_cost_kib=16, parallelism=1))
    stored = weaker.hash(VALID_PASSWORD)

    matches, upgraded = stronger.verify_and_update(VALID_PASSWORD, stored)

    assert matches
    assert upgraded is not None
    assert "m=16,t=2,p=1" in upgraded


def test_unreadable_hashes_do_not_match_instead_of_failing() -> None:
    too_long_for_bcrypt = "a" * 100
    legacy = LEGACY_BCRYPT.hash(VALID_PASSWORD)

    assert TEST_HASHER.verify_and_update(VALID_PASSWORD, "no-es-un-hash") == (False, None)
    assert TEST_HASHER.verify_and_update(VALID_PASSWORD, "") == (False, None)
    assert TEST_HASHER.verify_and_update(too_long_for_bcrypt, legacy) == (False, None)


def test_the_dummy_hash_is_argon2id_and_never_matches() -> None:
    dummy = TEST_HASHER.dummy_hash()

    assert dummy.startswith(ARGON2ID)
    assert dummy == TEST_HASHER.dummy_hash()
    assert TEST_HASHER.verify_and_update(VALID_PASSWORD, dummy) == (False, None)


def test_the_cost_comes_from_settings() -> None:
    hasher = hasher_from_settings(
        Settings(
            password_argon2_time_cost=2,
            password_argon2_memory_kib=32,
            password_argon2_parallelism=2,
        )
    )

    assert "m=32,t=2,p=2" in hasher.hash(VALID_PASSWORD)


def test_the_production_cost_stays_within_a_reasonable_time() -> None:
    production = hasher_from_settings(Settings())

    started = time.perf_counter()
    stored = production.hash(VALID_PASSWORD)
    verified, _ = production.verify_and_update(VALID_PASSWORD, stored)
    elapsed = time.perf_counter() - started

    assert verified
    assert "m=65536,t=3,p=1" in stored
    # Hash y verificación juntos: holgado para un CI lento, lejos de un costo mal puesto.
    assert elapsed < 2.0


async def _stored_hash(session: AsyncSession, user_id: int) -> str:
    session.expire_all()
    return (
        await session.execute(select(UserRow.password_hash).where(UserRow.id == user_id))
    ).scalar_one()


async def test_login_rehashes_a_bcrypt_account_to_argon2id(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    user = restaurant_a.waiter
    assert user.id is not None
    await session.execute(
        update(UserRow)
        .where(UserRow.id == user.id)
        .values(password_hash=LEGACY_BCRYPT.hash(VALID_PASSWORD))
    )
    await session.commit()
    body = {"email": user.email, "password": VALID_PASSWORD}

    first = await client.post(url("auth/login"), json=body)
    migrated = await _stored_hash(session, user.id)
    second = await client.post(url("auth/login"), json=body)

    assert first.status_code == 200
    assert migrated.startswith(ARGON2ID)
    assert second.status_code == 200
    assert await _stored_hash(session, user.id) == migrated


async def test_a_failed_login_leaves_the_bcrypt_hash_untouched(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    user = restaurant_a.waiter
    assert user.id is not None
    legacy = LEGACY_BCRYPT.hash(VALID_PASSWORD)
    await session.execute(update(UserRow).where(UserRow.id == user.id).values(password_hash=legacy))
    await session.commit()

    response = await client.post(
        url("auth/login"), json={"email": user.email, "password": "otra-contrasena"}
    )

    assert response.status_code == 401
    assert await _stored_hash(session, user.id) == legacy


async def test_changing_the_password_stores_argon2id(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    user = restaurant_a.waiter
    assert user.id is not None

    response = await client.post(
        url("auth/me/password"),
        json={"current_password": VALID_PASSWORD, "new_password": "otra-contrasena-larga"},
        headers=authorization_for(user),
    )

    assert response.status_code == 200
    assert (await _stored_hash(session, user.id)).startswith(ARGON2ID)


async def test_platform_login_rehashes_a_bcrypt_account_to_argon2id(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=LEGACY_BCRYPT.hash(VALID_PASSWORD),
        )
    )
    await session.commit()

    response = await client.post(
        url("platform/auth/login"), json={"email": admin.email, "password": VALID_PASSWORD}
    )

    session.expire_all()
    stored = (
        await session.execute(
            select(PlatformAdminRow.password_hash).where(PlatformAdminRow.id == admin.id)
        )
    ).scalar_one()
    assert response.status_code == 200
    assert stored.startswith(ARGON2ID)
