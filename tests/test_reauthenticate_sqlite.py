"""Reautenticarse no traba SQLite cuando la petición ya escribió (fuzzing de la API).

La identificación anota el uso de la sesión (`last_seen_at`) en la sesión de la
petición, una vez por minuto. El límite de intentos corre en una sesión propia
(`SqlLoginAttemptStore`): en una SQLite de archivo, esa segunda sesión esperaba el
candado de escritura que la primera retenía hasta el final de la misma petición, y
la reautenticación respondía 500 «database is locked». Las demás pruebas comparten
una sola conexión en memoria (`StaticPool`) y no lo ven; esta usa un archivo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from resthub.core.database import Base
from tests.conftest import (
    DATABASE_UNDER_TEST,
    VALID_PASSWORD,
    FakeClock,
    StaffedRestaurant,
    url,
)

pytestmark = pytest.mark.skipif(
    DATABASE_UNDER_TEST.is_postgres, reason="El candado de escritura es propio de SQLite."
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    # Un segundo de espera en vez de cinco: si vuelve el candado, falla rápido.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'resthub.db'}", connect_args={"timeout": 1}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as open_session:
        yield open_session
    await engine.dispose()


async def test_reauthenticate_after_the_session_was_touched(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant, clock: FakeClock
) -> None:
    await session.commit()
    login = await client.post(
        url("auth/login"), json={"email": restaurant_a.admin.email, "password": VALID_PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    # Pasado el minuto, la identificación vuelve a anotar el uso de la sesión.
    clock.advance(120)

    response = await client.post(
        url("auth/me/reauthenticate"), json={"password": VALID_PASSWORD}, headers=headers
    )

    assert response.status_code == 200, response.text
