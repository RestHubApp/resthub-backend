"""Administración del sistema por HTTP: acceso, restaurantes y bitácora."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import Permission
from resthub.modules.platform.adapters.persistence.models import PlatformAdminRow
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
LOGIN_URL = f"{API}/platform/auth/login"
ME_URL = f"{API}/platform/auth/me"
REFRESH_URL = f"{API}/platform/auth/refresh"
RESTAURANTS_URL = f"{API}/platform/restaurants"
ACTIVITY_URL = f"{API}/platform/activity"
ADMIN_EMAIL = "equipo@resthub.dev"


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


def platform_authorization_for(admin: PlatformAdmin) -> dict[str, str]:
    token = TEST_TOKEN_SERVICE.issue_platform(admin.id or 0)
    return {"Authorization": f"Bearer {token.value}"}


@pytest.fixture
def headers(platform_admin: PlatformAdmin) -> dict[str, str]:
    return platform_authorization_for(platform_admin)


def _new_restaurant(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Cevichería Doña Rosa",
        "slug": "dona-rosa",
        "timezone": "America/Lima",
        "owner": {
            "full_name": "Rosa Pérez",
            "email": "rosa@donarosa.pe",
            "password": VALID_PASSWORD,
        },
    }
    return {**body, **changes}


async def _activity_kinds(client: AsyncClient, headers: dict[str, str]) -> list[str]:
    response = await client.get(ACTIVITY_URL, headers=headers)
    assert response.status_code == 200
    return [item["kind"] for item in response.json()["items"]]


# --- Acceso -----------------------------------------------------------------


async def test_login_devuelve_un_token_de_plataforma_y_la_cuenta(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "EQUIPO@resthub.dev", "password": VALID_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["admin"] == {
        "id": platform_admin.id,
        "full_name": "Equipo RestHub",
        "email": ADMIN_EMAIL,
    }
    claims = TEST_TOKEN_SERVICE.decode_platform(body["access_token"])
    assert claims.admin_id == platform_admin.id
    payload = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert payload["scope"] == "platform"
    assert "restaurant_id" not in payload


async def test_el_acceso_queda_en_la_bitacora_de_plataforma(
    client: AsyncClient, platform_admin: PlatformAdmin
) -> None:
    login = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.get(ACTIVITY_URL, headers=headers)

    assert response.status_code == 200
    [entry] = response.json()["items"]
    assert entry["kind"] == "signed_in"
    assert entry["kind_label"] == "Inició sesión"
    assert (entry["admin_id"], entry["admin_name"]) == (platform_admin.id, "Equipo RestHub")


@pytest.mark.parametrize(
    ("email", "password"),
    [(ADMIN_EMAIL, "otra-contrasena"), ("nadie@resthub.dev", VALID_PASSWORD)],
)
async def test_un_acceso_fallido_responde_401_generico(
    client: AsyncClient, platform_admin: PlatformAdmin, email: str, password: str
) -> None:
    response = await client.post(LOGIN_URL, json={"email": email, "password": password})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_una_cuenta_de_plataforma_desactivada_responde_igual(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    await session.execute(update(PlatformAdminRow).values(is_active=False))
    await session.commit()

    response = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})

    assert response.status_code == 401
    assert response.json()["detail"] == "El correo o la contraseña no son correctos."


async def test_una_cuenta_del_personal_no_entra_por_el_acceso_de_plataforma(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.post(
        LOGIN_URL, json={"email": "encargado@local-a.pe", "password": VALID_PASSWORD}
    )

    assert response.status_code == 401


async def test_cinco_fallos_bloquean_el_acceso_de_plataforma_y_no_el_del_personal(
    client: AsyncClient, platform_admin: PlatformAdmin, local_a: StaffedRestaurant
) -> None:
    for _ in range(5):
        fallo = await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": "mala-mala"})
        assert fallo.status_code == 401

    bloqueado = await client.post(
        LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD}
    )
    personal = await client.post(
        f"{API}/auth/login", json={"email": "encargado@local-a.pe", "password": VALID_PASSWORD}
    )

    assert bloqueado.status_code == 429
    assert int(bloqueado.headers["retry-after"]) > 0
    assert personal.status_code == 200


async def test_me_y_refresh_devuelven_la_cuenta(
    client: AsyncClient, platform_admin: PlatformAdmin, headers: dict[str, str]
) -> None:
    me = await client.get(ME_URL, headers=headers)
    refresh = await client.post(REFRESH_URL, headers=headers)

    assert me.status_code == 200
    assert me.json() == {
        "admin": {"id": platform_admin.id, "full_name": "Equipo RestHub", "email": ADMIN_EMAIL}
    }
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["admin"] == me.json()["admin"]
    assert TEST_TOKEN_SERVICE.decode_platform(body["access_token"]).admin_id == platform_admin.id
    renewed = await client.get(ME_URL, headers={"Authorization": f"Bearer {body['access_token']}"})
    assert renewed.status_code == 200


async def test_me_sin_credencial_responde_401(client: AsyncClient) -> None:
    response = await client.get(ME_URL)

    assert response.status_code == 401


async def test_desactivar_la_cuenta_corta_un_token_ya_emitido(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    await session.execute(update(PlatformAdminRow).values(is_active=False))
    await session.commit()

    response = await client.get(ME_URL, headers=headers)

    assert response.status_code == 401


# --- Un token no sirve del otro lado ------------------------------------------


@pytest.mark.parametrize("path", ["/auth/me", "/restaurant", "/staff", "/orders"])
async def test_un_token_de_plataforma_no_sirve_en_un_restaurante(
    client: AsyncClient, local_a: StaffedRestaurant, headers: dict[str, str], path: str
) -> None:
    response = await client.get(f"{API}{path}", headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path", ["/platform/auth/me", "/platform/restaurants", "/platform/activity"]
)
async def test_un_token_de_restaurante_no_sirve_en_la_plataforma(
    client: AsyncClient, platform_admin: PlatformAdmin, local_a: StaffedRestaurant, path: str
) -> None:
    # El encargado tiene todos los permisos del local, y aun así no pasa.
    response = await client.get(f"{API}{path}", headers=authorization_for(local_a.admin))

    assert response.status_code == 401


async def test_un_token_de_restaurante_sin_alcance_sigue_sirviendo_solo_en_el_local(
    client: AsyncClient, platform_admin: PlatformAdmin, local_a: StaffedRestaurant
) -> None:
    """Los tokens emitidos antes de la plataforma no traen `scope`."""
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(local_a.admin.id),
            "restaurant_id": local_a.id,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        "secreto-de-prueba-con-largo-suficiente",
        algorithm="HS256",
    )
    viejo = {"Authorization": f"Bearer {token}"}
    # Mismo número de cuenta en las dos tablas: solo el alcance las separa.
    assert local_a.admin.id == platform_admin.id

    local = await client.get(f"{API}/auth/me", headers=viejo)
    plataforma = await client.get(ME_URL, headers=viejo)

    assert local.status_code == 200
    assert plataforma.status_code == 401


# --- Restaurantes ---------------------------------------------------------------


async def test_lista_los_restaurantes_con_su_personal_lo_mas_nuevo_primero(
    client: AsyncClient,
    session: AsyncSession,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    await session.execute(
        text("UPDATE users SET is_active = :inactive WHERE id = :id"),
        {"inactive": False, "id": local_b.waiter.id},
    )
    await session.execute(
        update(RestaurantRow)
        .where(RestaurantRow.id == local_a.id)
        .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await session.commit()

    response = await client.get(RESTAURANTS_URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["slug"] for item in body["items"]] == ["local-b", "local-a"]
    primero = body["items"][0]
    assert set(primero) == {
        "id",
        "name",
        "slug",
        "timezone",
        "is_active",
        "created_at",
        "staff_count",
        "active_staff_count",
    }
    assert (primero["staff_count"], primero["active_staff_count"]) == (2, 1)
    assert (primero["timezone"], primero["is_active"]) == ("America/Lima", True)


@pytest.mark.parametrize(("search", "slugs"), [("LOCAL-A", ["local-a"]), ("restaurante", None)])
async def test_busca_por_nombre_o_identificador_sin_mayusculas(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    headers: dict[str, str],
    search: str,
    slugs: list[str] | None,
) -> None:
    response = await client.get(RESTAURANTS_URL, params={"search": search}, headers=headers)

    body = response.json()
    if slugs is None:
        assert body["total"] == 2
    else:
        assert [item["slug"] for item in body["items"]] == slugs
        assert body["total"] == 1


async def test_un_comodin_en_la_busqueda_se_busca_tal_cual(
    client: AsyncClient, local_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.get(RESTAURANTS_URL, params={"search": "%"}, headers=headers)

    assert response.json() == {"items": [], "total": 0}


async def test_pagina_los_restaurantes(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    primera = await client.get(RESTAURANTS_URL, params={"limit": 1}, headers=headers)
    segunda = await client.get(RESTAURANTS_URL, params={"limit": 1, "offset": 1}, headers=headers)

    assert primera.json()["total"] == segunda.json()["total"] == 2
    ids = [primera.json()["items"][0]["id"], segunda.json()["items"][0]["id"]]
    assert sorted(ids) == sorted([local_a.id, local_b.id])


async def test_dar_de_alta_crea_restaurante_roles_base_y_encargado(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    response = await client.post(RESTAURANTS_URL, json=_new_restaurant(), headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["slug"], body["timezone"]) == (
        "Cevichería Doña Rosa",
        "dona-rosa",
        "America/Lima",
    )
    assert (body["is_active"], body["staff_count"], body["active_staff_count"]) == (True, 1, 1)
    [owner] = body["owners"]
    assert owner["email"] == "rosa@donarosa.pe"
    assert (owner["full_name"], owner["is_active"]) == ("Rosa Pérez", True)
    roles = (
        await session.execute(
            text("SELECT name, kind FROM roles WHERE restaurant_id = :id ORDER BY kind"),
            {"id": body["id"]},
        )
    ).all()
    assert [tuple(role) for role in roles] == [("Encargado", "owner"), ("Mesero", "waiter")]


async def test_el_encargado_creado_entra_con_todos_los_permisos(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    created = await client.post(RESTAURANTS_URL, json=_new_restaurant(), headers=headers)

    login = await client.post(
        f"{API}/auth/login", json={"email": "rosa@donarosa.pe", "password": VALID_PASSWORD}
    )

    assert login.status_code == 200
    body = login.json()
    assert body["restaurant"]["id"] == created.json()["id"]
    assert body["user"]["role_label"] == "Encargado"
    assert body["permissions"] == sorted(permission.value for permission in Permission)


async def test_el_alta_queda_en_la_bitacora(client: AsyncClient, headers: dict[str, str]) -> None:
    await client.post(RESTAURANTS_URL, json=_new_restaurant(), headers=headers)

    response = await client.get(ACTIVITY_URL, headers=headers)

    [entry] = response.json()["items"]
    assert entry["kind"] == "restaurant_created"
    assert "dona-rosa" in entry["detail"]
    assert "rosa@donarosa.pe" in entry["detail"]


async def test_un_identificador_repetido_responde_409(
    client: AsyncClient, local_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.post(
        RESTAURANTS_URL, json=_new_restaurant(slug="local-a"), headers=headers
    )

    assert response.status_code == 409


async def test_un_correo_ya_usado_responde_409_y_no_deja_el_restaurante(
    client: AsyncClient, local_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    owner = {"full_name": "Otra", "email": "mesero@local-a.pe", "password": VALID_PASSWORD}

    response = await client.post(
        RESTAURANTS_URL, json=_new_restaurant(owner=owner), headers=headers
    )
    listado = await client.get(RESTAURANTS_URL, params={"search": "dona"}, headers=headers)

    assert response.status_code == 409
    # Una sola transacción: sin encargado no queda un restaurante a medias.
    assert listado.json()["total"] == 0
    assert await _activity_kinds(client, headers) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "America/Atlantida"},
        {"slug": "Doña Rosa"},
        {"slug": "-dona-"},
        {
            "owner": {
                "full_name": "Rosa",
                "email": "rosa@donarosa.pe",
                "password": "corta",
            }
        },
    ],
)
async def test_datos_invalidos_responden_422(
    client: AsyncClient, headers: dict[str, str], changes: dict[str, object]
) -> None:
    response = await client.post(RESTAURANTS_URL, json=_new_restaurant(**changes), headers=headers)
    listado = await client.get(RESTAURANTS_URL, headers=headers)

    assert response.status_code == 422
    assert listado.json()["total"] == 0


async def test_la_ficha_trae_solo_a_los_encargados(
    client: AsyncClient, local_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.get(f"{RESTAURANTS_URL}/{local_a.id}", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "local-a"
    assert body["staff_count"] == 2
    assert body["owners"] == [
        {
            "id": local_a.admin.id,
            "full_name": "Rosa Pérez",
            "email": "encargado@local-a.pe",
            "is_active": True,
        }
    ]


async def test_la_ficha_de_un_restaurante_que_no_existe_responde_404(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    ficha = await client.get(f"{RESTAURANTS_URL}/999", headers=headers)
    editar = await client.patch(f"{RESTAURANTS_URL}/999", json={"name": "X"}, headers=headers)
    encargado = await client.post(
        f"{RESTAURANTS_URL}/999/owners",
        json={"full_name": "X", "email": "x@x.pe", "password": VALID_PASSWORD},
        headers=headers,
    )

    assert (ficha.status_code, editar.status_code, encargado.status_code) == (404, 404, 404)


async def test_edita_nombre_y_zona_horaria(
    client: AsyncClient, local_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.patch(
        f"{RESTAURANTS_URL}/{local_a.id}",
        json={"name": "  Nuevo   nombre ", "timezone": "America/Bogota"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["timezone"], body["is_active"]) == (
        "Nuevo nombre",
        "America/Bogota",
        True,
    )
    assert len(body["owners"]) == 1
    [entry] = (await client.get(ACTIVITY_URL, headers=headers)).json()["items"]
    assert entry["kind"] == "restaurant_updated"
    assert entry["detail"] == "local-a: nombre «Nuevo nombre», zona horaria America/Bogota"


async def test_editar_con_una_zona_horaria_invalida_responde_422(
    client: AsyncClient, local_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.patch(
        f"{RESTAURANTS_URL}/{local_a.id}", json={"timezone": "Lima"}, headers=headers
    )

    assert response.status_code == 422


async def test_desactivar_corta_el_acceso_del_personal_y_reactivar_lo_devuelve(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    encargado, mesero = authorization_for(local_a.admin), authorization_for(local_a.waiter)
    otro_local = authorization_for(local_b.admin)

    apagado = await client.patch(
        f"{RESTAURANTS_URL}/{local_a.id}", json={"is_active": False}, headers=headers
    )
    assert apagado.status_code == 200
    assert apagado.json()["is_active"] is False
    assert (await client.get(f"{API}/auth/me", headers=encargado)).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers=mesero)).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers=otro_local)).status_code == 200

    prendido = await client.patch(
        f"{RESTAURANTS_URL}/{local_a.id}", json={"is_active": True}, headers=headers
    )
    assert prendido.json()["is_active"] is True
    assert (await client.get(f"{API}/auth/me", headers=encargado)).status_code == 200

    detalles = [
        item["detail"] for item in (await client.get(ACTIVITY_URL, headers=headers)).json()["items"]
    ]
    assert detalles == ["local-a: activado", "local-a: desactivado"]


async def test_editar_sin_cambios_no_deja_asiento(
    client: AsyncClient, local_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.patch(
        f"{RESTAURANTS_URL}/{local_a.id}", json={"is_active": True}, headers=headers
    )

    assert response.status_code == 200
    assert await _activity_kinds(client, headers) == []


async def test_agrega_otro_encargado_que_puede_entrar(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant, headers: dict[str, str]
) -> None:
    response = await client.post(
        f"{RESTAURANTS_URL}/{local_a.id}/owners",
        json={"full_name": "Carla Ríos", "email": "Carla@Local-A.pe", "password": VALID_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["full_name"], body["email"], body["is_active"]) == (
        "Carla Ríos",
        "carla@local-a.pe",
        True,
    )
    ficha = await client.get(f"{RESTAURANTS_URL}/{local_a.id}", headers=headers)
    assert {owner["email"] for owner in ficha.json()["owners"]} == {
        "encargado@local-a.pe",
        "carla@local-a.pe",
    }
    login = await client.post(
        f"{API}/auth/login", json={"email": "carla@local-a.pe", "password": VALID_PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["restaurant"]["id"] == local_a.id
    assert "staff.manage" in login.json()["permissions"]
    [entry] = (await client.get(ACTIVITY_URL, headers=headers)).json()["items"]
    assert (entry["kind"], entry["detail"]) == ("owner_added", "carla@local-a.pe en local-a")


async def test_agregar_un_encargado_con_un_correo_usado_responde_409(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    headers: dict[str, str],
) -> None:
    response = await client.post(
        f"{RESTAURANTS_URL}/{local_a.id}/owners",
        json={"full_name": "Luis", "email": "mesero@local-b.pe", "password": VALID_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 409


# --- Bitácora -----------------------------------------------------------------


async def test_la_bitacora_se_pagina_con_lo_ultimo_primero(
    client: AsyncClient, session: AsyncSession, platform_admin: PlatformAdmin
) -> None:
    for _ in range(3):
        await client.post(LOGIN_URL, json={"email": ADMIN_EMAIL, "password": VALID_PASSWORD})
    headers = platform_authorization_for(platform_admin)
    await client.post(RESTAURANTS_URL, json=_new_restaurant(), headers=headers)

    response = await client.get(ACTIVITY_URL, params={"limit": 2}, headers=headers)

    body = response.json()
    assert body["total"] == 4
    assert [item["kind"] for item in body["items"]] == ["restaurant_created", "signed_in"]
    assert set(body["items"][0]) == {
        "id",
        "admin_id",
        "admin_name",
        "kind",
        "kind_label",
        "detail",
        "created_at",
    }
    ids = (await session.execute(select(PlatformAdminRow.id))).scalars().all()
    assert ids == [platform_admin.id]


async def test_una_contrasena_de_mas_de_72_bytes_se_rechaza_sin_500(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    body = {
        "name": "Local largo",
        "slug": "local-largo",
        "timezone": "America/Lima",
        # 30 caracteres, pero 120 bytes: bcrypt no los acepta.
        "owner": {"full_name": "Ana Paz", "email": "ana@largo.pe", "password": "🍋" * 30},
    }

    response = await client.post("/api/v1/platform/restaurants", json=body, headers=headers)

    assert response.status_code == 422, response.text


async def test_un_identificador_fuera_de_rango_es_422_y_no_500(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    grande = await client.get("/api/v1/platform/restaurants/3000000000", headers=headers)
    lejos = await client.get(
        "/api/v1/platform/restaurants", params={"offset": 10**20}, headers=headers
    )

    assert grande.status_code == 422
    assert lejos.status_code == 422
