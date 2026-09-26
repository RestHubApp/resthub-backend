"""Canje de códigos de vista previa y la sesión que abren (módulo `accounts`).

Los códigos se emiten acá con el caso de uso, sin pasar por la plataforma: lo
que se prueba es el canje, el token y lo que ese token puede o no puede hacer.
El camino completo desde `/platform/preview` está en `test_api_sandbox.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from resthub.core.database import Base
from resthub.core.permissions import Permission
from resthub.modules.accounts.adapters.persistence.directories import SqlRestaurantDirectory
from resthub.modules.accounts.adapters.persistence.models import PreviewCodeRow
from resthub.modules.accounts.adapters.persistence.sqlalchemy_preview_codes import (
    SqlAlchemyPreviewCodeRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.exceptions import NotASandboxAccount
from resthub.modules.accounts.domain.preview import PreviewCode, PreviewGrant, preview_code_hash
from resthub.modules.accounts.use_cases.preview import IssuePreviewCode, IssuePreviewCodeCommand
from resthub.modules.platform.adapters.persistence.models import PlatformAdminRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from tests.conftest import (
    REGISTERED_MODELS,
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    FakeClock,
    StaffedRestaurant,
    authorization_for,
    staffed_restaurant,
)

API = "/api/v1"
PREVIEW_URL = f"{API}/auth/preview"
ME_URL = f"{API}/auth/me"

assert REGISTERED_MODELS


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


async def _issue(session: AsyncSession, user_id: int, admin_id: int) -> str:
    issued = await IssuePreviewCode(
        SqlAlchemyPreviewCodeRepository(session),
        SqlAlchemyUserRepository(session),
        SqlRestaurantDirectory(session),
        lambda: datetime.now(UTC),
    )(IssuePreviewCodeCommand(user_id=user_id, platform_admin_id=admin_id))
    await session.commit()
    assert issued.expires_in_seconds == 60
    return issued.code


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _deactivate_admin(session: AsyncSession, admin_id: int) -> None:
    await session.execute(
        update(PlatformAdminRow).where(PlatformAdminRow.id == admin_id).values(is_active=False)
    )
    await session.commit()


# --- Emisión ----------------------------------------------------------------


async def test_el_codigo_es_largo_aleatorio_y_se_guarda_con_hash(
    session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)
    otro = await _issue(session, sandbox.admin.id or 0, admin_id)

    assert len(code) >= 43
    assert code != otro
    rows = (await session.execute(select(PreviewCodeRow))).scalars().all()
    assert {row.code_hash for row in rows} == {preview_code_hash(code), preview_code_hash(otro)}
    assert all(code not in row.code_hash for row in rows)
    assert {(row.user_id, row.platform_admin_id, row.used_at) for row in rows} == {
        (sandbox.admin.id, admin_id, None)
    }


async def test_emitir_un_codigo_borra_los_de_hace_mas_de_un_dia(
    session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    now = datetime.now(UTC)
    codes = SqlAlchemyPreviewCodeRepository(session)
    for name, age in (("viejo", timedelta(days=2)), ("de-hoy", timedelta(hours=3))):
        await codes.add(
            PreviewCode(
                code_hash=preview_code_hash(name),
                user_id=sandbox.admin.id or 0,
                platform_admin_id=admin_id,
                expires_at=now - age + timedelta(seconds=60),
                created_at=now - age,
            )
        )
    await session.commit()

    nuevo = await _issue(session, sandbox.admin.id or 0, admin_id)

    hashes = set((await session.execute(select(PreviewCodeRow.code_hash))).scalars())
    assert hashes == {preview_code_hash("de-hoy"), preview_code_hash(nuevo)}


async def test_no_se_emite_un_codigo_para_una_cuenta_de_un_local_real(
    session: AsyncSession, local_a: StaffedRestaurant, admin_id: int
) -> None:
    with pytest.raises(NotASandboxAccount):
        await _issue(session, local_a.admin.id or 0, admin_id)
    assert (await session.execute(select(PreviewCodeRow))).first() is None


# --- Canje ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cuenta", "puede", "no_puede"),
    [
        ("admin", Permission.STAFF_MANAGE, None),
        ("waiter", Permission.ORDERS_TAKE, Permission.STAFF_MANAGE),
    ],
)
async def test_el_canje_abre_la_sesion_de_esa_cuenta_con_sus_permisos(
    client: AsyncClient,
    session: AsyncSession,
    sandbox: StaffedRestaurant,
    admin_id: int,
    cuenta: str,
    puede: Permission,
    no_puede: Permission | None,
) -> None:
    user = getattr(sandbox, cuenta)
    code = await _issue(session, user.id or 0, admin_id)

    response = await client.post(PREVIEW_URL, json={"code": code})

    assert response.status_code == 200
    body = response.json()
    assert body["preview"] is True
    assert body["user"]["id"] == user.id
    assert body["restaurant"]["id"] == sandbox.id
    assert body["expires_in"] == 30 * 60
    assert puede in body["permissions"]
    if no_puede is not None:
        assert no_puede not in body["permissions"]
    payload = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert payload["preview"] is True
    assert payload["platform_admin_id"] == admin_id
    assert payload["scope"] == "restaurant"

    me = await client.get(ME_URL, headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["preview"] is True
    assert me.json()["permissions"] == body["permissions"]


async def test_el_token_de_vista_previa_opera_segun_los_permisos_del_rol(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.waiter.id or 0, admin_id)
    token = (await client.post(PREVIEW_URL, json={"code": code})).json()["access_token"]

    assert (await client.get(f"{API}/tables", headers=_bearer(token))).status_code == 200
    assert (await client.get(f"{API}/staff", headers=_bearer(token))).status_code == 403


async def test_un_codigo_sirve_una_sola_vez(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)

    primera = await client.post(PREVIEW_URL, json={"code": code})
    segunda = await client.post(PREVIEW_URL, json={"code": code})

    assert primera.status_code == 200
    assert segunda.status_code == 401
    assert segunda.headers["www-authenticate"] == "Bearer"
    assert segunda.json()["detail"] == "El código de vista previa no es válido o ya venció."


async def test_un_codigo_vencido_responde_401(
    client: AsyncClient,
    session: AsyncSession,
    sandbox: StaffedRestaurant,
    admin_id: int,
    clock: FakeClock,
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)

    clock.advance(61)
    response = await client.post(PREVIEW_URL, json={"code": code})

    assert response.status_code == 401
    assert response.json()["detail"] == "El código de vista previa no es válido o ya venció."


async def test_un_codigo_todavia_vigente_entra(
    client: AsyncClient,
    session: AsyncSession,
    sandbox: StaffedRestaurant,
    admin_id: int,
    clock: FakeClock,
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)

    clock.advance(50)

    assert (await client.post(PREVIEW_URL, json={"code": code})).status_code == 200


async def test_un_codigo_de_una_cuenta_de_plataforma_desactivada_no_entra(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)
    await _deactivate_admin(session, admin_id)

    response = await client.post(PREVIEW_URL, json={"code": code})

    assert response.status_code == 401
    assert response.json()["detail"] == "El código de vista previa no es válido o ya venció."


async def test_desactivar_la_cuenta_de_plataforma_corta_sus_vistas_previas(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)
    token = (await client.post(PREVIEW_URL, json={"code": code})).json()["access_token"]
    assert (await client.get(ME_URL, headers=_bearer(token))).status_code == 200

    await _deactivate_admin(session, admin_id)
    response = await client.get(ME_URL, headers=_bearer(token))

    assert response.status_code == 401
    assert response.json()["detail"] == (
        "La cuenta de plataforma de esta vista previa ya no está activa."
    )
    # La sesión común de esa misma cuenta no depende de la plataforma.
    assert (await client.get(ME_URL, headers=authorization_for(sandbox.admin))).status_code == 200


async def test_un_token_de_vista_previa_de_una_cuenta_de_plataforma_que_no_existe_se_rechaza(
    client: AsyncClient, sandbox: StaffedRestaurant
) -> None:
    token = TEST_TOKEN_SERVICE.issue_preview(
        sandbox.admin.id or 0, sandbox.id, platform_admin_id=999
    )

    response = await client.get(ME_URL, headers=_bearer(token.value))

    assert response.status_code == 401


@pytest.mark.parametrize("code", ["x", "a" * 43, "a" * 128])
async def test_un_codigo_desconocido_responde_401(
    client: AsyncClient, sandbox: StaffedRestaurant, code: str
) -> None:
    response = await client.post(PREVIEW_URL, json={"code": code})

    assert response.status_code == 401


async def test_un_codigo_de_una_cuenta_que_dejo_de_ser_de_muestra_no_entra(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant, admin_id: int
) -> None:
    """Aunque un código apareciera para una cuenta real, el canje lo rechaza."""
    code = "codigo-plantado-a-mano-para-una-cuenta-real"
    await SqlAlchemyPreviewCodeRepository(session).add(
        PreviewCode(
            code_hash=preview_code_hash(code),
            user_id=local_a.admin.id or 0,
            platform_admin_id=admin_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()

    response = await client.post(PREVIEW_URL, json={"code": code})

    assert response.status_code == 401


async def test_el_canje_queda_en_la_bitacora_del_local_de_muestra(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.waiter.id or 0, admin_id)
    await client.post(PREVIEW_URL, json={"code": code})

    response = await client.get(f"{API}/activity", headers=authorization_for(sandbox.admin))

    [entry] = response.json()["items"]
    assert (entry["kind"], entry["detail"], entry["user_id"]) == (
        "signed_in",
        "Vista previa",
        sandbox.waiter.id,
    )


async def test_dos_canjes_a_la_vez_dan_uno_solo_que_funciona(tmp_path: Path) -> None:
    """Dos sesiones sobre la misma base, como dos pestañas que canjean el mismo código."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'carrera.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        sandbox = await staffed_restaurant(setup, "muestra", is_sandbox=True)
        admin = await SqlAlchemyPlatformAdminRepository(setup).add(
            PlatformAdmin(email="equipo@resthub.dev", full_name="Equipo", password_hash="x")
        )
        code = await _issue(setup, sandbox.admin.id or 0, admin.id or 0)

    async def canjear() -> PreviewGrant | None:
        async with factory() as session:
            grant = await SqlAlchemyPreviewCodeRepository(session).consume(
                preview_code_hash(code), datetime.now(UTC)
            )
            await session.commit()
            return grant

    try:
        resultados = await asyncio.gather(canjear(), canjear(), canjear())
    finally:
        await engine.dispose()

    assert sorted(resultados, key=lambda grant: grant is None) == [
        PreviewGrant(user_id=sandbox.admin.id or 0, platform_admin_id=admin.id or 0),
        None,
        None,
    ]


# --- Lo que la vista previa no hace -----------------------------------------


async def test_una_vista_previa_no_se_renueva(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)
    token = (await client.post(PREVIEW_URL, json={"code": code})).json()["access_token"]

    response = await client.post(f"{API}/auth/refresh", headers=_bearer(token))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_en_la_vista_previa_no_se_cambia_la_contrasena(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)
    token = (await client.post(PREVIEW_URL, json={"code": code})).json()["access_token"]

    response = await client.post(
        f"{API}/auth/me/password",
        headers=_bearer(token),
        json={"current_password": VALID_PASSWORD, "new_password": "otra-contrasena-larga"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "En la vista previa no se cambia la contraseña."


async def test_un_token_de_vista_previa_de_una_cuenta_real_se_rechaza(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    """Defensa en profundidad: nadie lo emite, pero si apareciera no entra."""
    token = TEST_TOKEN_SERVICE.issue_preview(local_a.admin.id or 0, local_a.id, platform_admin_id=1)

    response = await client.get(ME_URL, headers=_bearer(token.value))

    assert response.status_code == 401
    assert response.json()["detail"] == "La vista previa solo entra al local de muestra."


async def test_un_token_de_vista_previa_no_sirve_en_la_plataforma(
    client: AsyncClient, session: AsyncSession, sandbox: StaffedRestaurant, admin_id: int
) -> None:
    code = await _issue(session, sandbox.admin.id or 0, admin_id)
    token = (await client.post(PREVIEW_URL, json={"code": code})).json()["access_token"]

    for url in (f"{API}/platform/auth/me", f"{API}/platform/restaurants"):
        assert (await client.get(url, headers=_bearer(token))).status_code == 401


async def test_una_sesion_comun_dice_que_no_es_vista_previa(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    login = await client.post(
        f"{API}/auth/login", json={"email": local_a.admin.email, "password": VALID_PASSWORD}
    )
    me = await client.get(ME_URL, headers=authorization_for(local_a.admin))

    assert login.json()["preview"] is False
    assert me.json()["preview"] is False


async def test_al_local_de_muestra_no_se_entra_con_contrasena(
    client: AsyncClient, sandbox: StaffedRestaurant
) -> None:
    """Ni siquiera con una cuenta a la que alguien le puso una contraseña conocida."""
    response = await client.post(
        f"{API}/auth/login", json={"email": sandbox.admin.email, "password": VALID_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."
