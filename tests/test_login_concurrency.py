"""El límite de intentos de acceso aguanta una ráfaga de peticiones simultáneas.

Cada petición usa su propia sesión de la base, como en producción: con la
sesión compartida de las demás pruebas las peticiones no se solaparían.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.database import get_session
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import TEST_HASHER, VALID_PASSWORD, StaffedRestaurant

BURST = 30
ATTACKER = {"x-forwarded-for": "200.48.1.7"}


@pytest.fixture
def per_request_sessions(app: FastAPI, session: AsyncSession) -> None:
    factory = async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)

    async def own_session() -> AsyncIterator[AsyncSession]:
        async with factory() as request_session:
            try:
                yield request_session
                await request_session.commit()
            except Exception:
                await request_session.rollback()
                raise

    app.dependency_overrides[get_session] = own_session


async def _burst(client: AsyncClient, url: str, email: str) -> Counter[int]:
    responses = await asyncio.gather(
        *(
            client.post(url, json={"email": email, "password": f"no-es-esta-{n}"}, headers=ATTACKER)
            for n in range(BURST)
        )
    )
    return Counter(response.status_code for response in responses)


@pytest.mark.usefixtures("per_request_sessions")
async def test_una_rafaga_simultanea_no_evalua_mas_de_cinco_contrasenas(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    codes = await _burst(client, "/api/v1/auth/login", local_a.admin.email)
    correcta = await client.post(
        "/api/v1/auth/login",
        json={"email": local_a.admin.email, "password": VALID_PASSWORD},
        headers=ATTACKER,
    )

    assert codes == Counter({401: 5, 429: BURST - 5})
    # Ni la contraseña correcta pasa: el par quedó bloqueado.
    assert correcta.status_code == 429


@pytest.mark.usefixtures("per_request_sessions")
async def test_una_rafaga_simultanea_contra_la_plataforma_tampoco(
    client: AsyncClient, session: AsyncSession
) -> None:
    await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()

    codes = await _burst(client, "/api/v1/platform/auth/login", "equipo@resthub.dev")

    assert codes == Counter({401: 5, 429: BURST - 5})


@pytest.mark.usefixtures("per_request_sessions")
async def test_un_acceso_correcto_libera_su_intento(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    body = {"email": local_a.waiter.email, "password": VALID_PASSWORD}
    for _ in range(8):
        assert (await client.post("/api/v1/auth/login", json=body)).status_code == 200
