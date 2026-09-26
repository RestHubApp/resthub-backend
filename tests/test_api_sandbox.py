"""El local de muestra y la vista previa desde la administración del sistema."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import Permission
from resthub.modules.accounts.adapters.persistence.models import UserRow
from resthub.modules.menu.adapters.persistence.models import MenuItemRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow
from tests.conftest import (
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
)

API = "/api/v1"
SANDBOX_URL = f"{API}/platform/sandbox"
RESET_URL = f"{SANDBOX_URL}/reset"
PREVIEW_URL = f"{API}/platform/preview"
EXCHANGE_URL = f"{API}/auth/preview"
ACTIVITY_URL = f"{API}/platform/activity"
RESTAURANTS_URL = f"{API}/platform/restaurants"


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


@pytest.fixture
def headers(platform_admin: PlatformAdmin) -> dict[str, str]:
    token = TEST_TOKEN_SERVICE.issue_platform(platform_admin.id or 0)
    return {"Authorization": f"Bearer {token.value}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _preview_token(client: AsyncClient, headers: dict[str, str], as_: str) -> str:
    issued = await client.post(PREVIEW_URL, headers=headers, json={"as": as_})
    assert issued.status_code == 201
    exchanged = await client.post(EXCHANGE_URL, json={"code": issued.json()["code"]})
    assert exchanged.status_code == 200
    return str(exchanged.json()["access_token"])


async def _activity(client: AsyncClient, headers: dict[str, str]) -> list[tuple[str, str]]:
    response = await client.get(ACTIVITY_URL, headers=headers)
    return [(item["kind"], item["detail"]) for item in response.json()["items"]]


# --- Local de muestra -------------------------------------------------------


async def test_sin_local_de_muestra_responde_null(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get(SANDBOX_URL, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"restaurant": None, "accounts": []}


async def test_reiniciar_crea_el_local_de_muestra_con_los_datos_de_muestra(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    response = await client.post(RESET_URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    restaurant = body["restaurant"]
    assert (restaurant["name"], restaurant["slug"], restaurant["is_active"]) == (
        "Restaurante de muestra",
        "muestra",
        True,
    )
    assert restaurant["staff_count"] == 3
    assert body["accounts"] == [
        {"kind": "owner", "role_label": "Encargado", "full_name": "Encargado de muestra"},
        {"kind": "waiter", "role_label": "Mesero", "full_name": "Mesero de muestra"},
        {"kind": "custom", "role_label": "Cocinero", "full_name": "Cocinero de muestra"},
    ]
    assert (await client.get(SANDBOX_URL, headers=headers)).json() == body

    row = await session.get(RestaurantRow, restaurant["id"])
    assert row is not None and row.is_sandbox
    platos = await session.execute(
        select(func.count())
        .select_from(MenuItemRow)
        .where(MenuItemRow.restaurant_id == restaurant["id"])
    )
    assert platos.scalar_one() == 20
    correos = set(
        (
            await session.execute(
                select(UserRow.email).where(UserRow.restaurant_id == restaurant["id"])
            )
        ).scalars()
    )
    assert correos and all(correo.endswith("@muestra.resthub.invalid") for correo in correos)
    [(kind, _)] = await _activity(client, headers)
    assert kind == "sandbox_reset"


async def test_reiniciar_archiva_el_anterior_y_crea_otro(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    primero = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]
    token_viejo = await _preview_token(client, headers, "owner")

    segundo = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]

    assert segundo["id"] != primero["id"]
    assert (segundo["slug"], segundo["is_active"]) == ("muestra", True)
    archivado = await session.get(RestaurantRow, primero["id"])
    assert archivado is not None
    await session.refresh(archivado)
    assert (archivado.slug, archivado.is_active, archivado.is_sandbox) == (
        f"muestra-archivado-{primero['id']}",
        False,
        True,
    )
    vigentes = await session.execute(
        select(func.count())
        .select_from(RestaurantRow)
        .where(RestaurantRow.is_sandbox.is_(True), RestaurantRow.is_active.is_(True))
    )
    assert vigentes.scalar_one() == 1
    # Una vista previa del local archivado deja de servir.
    assert (await client.get(f"{API}/auth/me", headers=_bearer(token_viejo))).status_code == 401
    assert (await _activity(client, headers))[0] == (
        "sandbox_reset",
        f"local de muestra #{segundo['id']}; el #{primero['id']} quedó archivado",
    )


async def test_la_lista_de_la_plataforma_no_incluye_locales_de_muestra(
    client: AsyncClient, headers: dict[str, str], local_a: StaffedRestaurant
) -> None:
    primero = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]
    segundo = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]

    listado = (await client.get(RESTAURANTS_URL, headers=headers)).json()
    busqueda = await client.get(RESTAURANTS_URL, headers=headers, params={"search": "muestra"})

    assert [item["id"] for item in listado["items"]] == [local_a.id]
    assert listado["total"] == 1
    assert busqueda.json() == {"items": [], "total": 0}
    for sandbox in (primero, segundo):
        ficha = await client.get(f"{RESTAURANTS_URL}/{sandbox['id']}", headers=headers)
        editar = await client.patch(
            f"{RESTAURANTS_URL}/{sandbox['id']}", headers=headers, json={"is_active": True}
        )
        assert (ficha.status_code, editar.status_code) == (404, 404)


# --- Vista previa -----------------------------------------------------------


async def test_pedir_la_vista_previa_crea_el_local_si_falta_y_da_un_codigo(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.post(PREVIEW_URL, headers=headers, json={"as": "owner"})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"code", "expires_in"}
    assert body["expires_in"] == 60
    assert len(body["code"]) >= 43
    sandbox = (await client.get(SANDBOX_URL, headers=headers)).json()["restaurant"]
    assert sandbox["name"] == "Restaurante de muestra"
    [(kind, detail)] = await _activity(client, headers)
    assert kind == "preview_started"
    assert detail == (
        f"como Encargado (Encargado de muestra), local de muestra #{sandbox['id']}, recién creado"
    )


@pytest.mark.parametrize(
    ("as_", "nombre", "rol", "puede", "no_puede"),
    [
        ("owner", "Encargado de muestra", "Encargado", Permission.STAFF_MANAGE, None),
        ("waiter", "Mesero de muestra", "Mesero", Permission.ORDERS_TAKE, Permission.STAFF_MANAGE),
    ],
)
async def test_la_vista_previa_entra_como_la_cuenta_de_muestra_con_sus_permisos(
    client: AsyncClient,
    headers: dict[str, str],
    as_: str,
    nombre: str,
    rol: str,
    puede: Permission,
    no_puede: Permission | None,
) -> None:
    issued = (await client.post(PREVIEW_URL, headers=headers, json={"as": as_})).json()

    response = await client.post(EXCHANGE_URL, json={"code": issued["code"]})

    assert response.status_code == 200
    body = response.json()
    assert body["preview"] is True
    assert (body["user"]["full_name"], body["user"]["role_label"]) == (nombre, rol)
    assert body["restaurant"]["name"] == "Restaurante de muestra"
    assert puede in body["permissions"]
    if no_puede is not None:
        assert no_puede not in body["permissions"]

    token = _bearer(body["access_token"])
    me = await client.get(f"{API}/auth/me", headers=token)
    assert me.json()["preview"] is True
    # Opera el local de muestra como cualquier cuenta: sus mesas, su carta.
    mesas = await client.get(f"{API}/tables", headers=token)
    assert mesas.status_code == 200
    assert len(mesas.json()) == 8
    assert (await client.get(f"{API}/restaurant", headers=token)).status_code == 200
    staff = await client.get(f"{API}/staff", headers=token)
    assert staff.status_code == (200 if as_ == "owner" else 403)


async def test_la_vista_previa_como_otro_tipo_de_cuenta_responde_422(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    for body in ({"as": "custom"}, {"as": "admin"}, {}, {"user_id": 1}):
        response = await client.post(PREVIEW_URL, headers=headers, json=body)
        assert response.status_code == 422


async def test_el_codigo_de_la_plataforma_sirve_una_sola_vez(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    code = (await client.post(PREVIEW_URL, headers=headers, json={"as": "waiter"})).json()["code"]

    assert (await client.post(EXCHANGE_URL, json={"code": code})).status_code == 200
    assert (await client.post(EXCHANGE_URL, json={"code": code})).status_code == 401


async def test_sin_cuenta_activa_de_ese_tipo_responde_409(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    sandbox = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]
    await session.execute(
        update(UserRow)
        .where(UserRow.restaurant_id == sandbox["id"], UserRow.full_name == "Mesero de muestra")
        .values(is_active=False)
    )
    await session.commit()

    response = await client.post(PREVIEW_URL, headers=headers, json={"as": "waiter"})

    assert response.status_code == 409
    assert (
        await client.post(PREVIEW_URL, headers=headers, json={"as": "owner"})
    ).status_code == 201


async def test_las_cuentas_de_muestra_no_entran_con_contrasena(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    sandbox = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]
    cuentas = (
        await session.execute(
            select(UserRow.email, UserRow.password_hash).where(
                UserRow.restaurant_id == sandbox["id"]
            )
        )
    ).all()
    assert len(cuentas) == 3

    for email, password_hash in cuentas:
        # El hash es de un valor aleatorio descartado: ninguna contraseña lo reproduce.
        for intento in ("resthub123", VALID_PASSWORD, ""):
            assert not TEST_HASHER.verify(intento, password_hash)
        response = await client.post(
            f"{API}/auth/login", json={"email": email, "password": "resthub123"}
        )
        # El dominio `.invalid` ni siquiera pasa como correo; y si pasara, el
        # acceso con contraseña a un local de muestra responde 401.
        assert response.status_code in (401, 422)
        assert "access_token" not in response.json()


# --- Alcance del token -------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", SANDBOX_URL, None),
        ("POST", RESET_URL, None),
        ("POST", PREVIEW_URL, {"as": "owner"}),
    ],
)
async def test_un_token_de_restaurante_no_llega_a_la_vista_previa(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    method: str,
    url: str,
    body: dict[str, str] | None,
) -> None:
    response = await client.request(
        method, url, headers=authorization_for(local_a.admin), json=body
    )

    assert response.status_code == 401


async def test_un_token_de_vista_previa_no_llega_a_la_vista_previa(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    token = _bearer(await _preview_token(client, headers, "owner"))

    assert (await client.get(SANDBOX_URL, headers=token)).status_code == 401
    assert (await client.post(PREVIEW_URL, headers=token, json={"as": "owner"})).status_code == 401


async def test_sin_credencial_responde_401(client: AsyncClient) -> None:
    assert (await client.get(SANDBOX_URL)).status_code == 401
    assert (await client.post(PREVIEW_URL, json={"as": "owner"})).status_code == 401
