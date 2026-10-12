"""El local de muestra y la vista previa desde la administración del sistema."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRow
from resthub.core.clock import SystemClock
from resthub.core.database import get_session
from resthub.core.permissions import Permission
from resthub.modules.accounts.adapters.persistence.models import PreviewCodeRow, UserRow
from resthub.modules.menu.adapters.persistence.models import MenuItemRow
from resthub.modules.platform.adapters.api.errors import to_http
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain import exceptions as platform_errors
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow
from resthub.wiring.sandbox import ModuleSandboxProvisioningGateway
from tests.conftest import (
    API_PREFIX,
    TEST_HASHER,
    TEST_TOKEN_SERVICE,
    VALID_PASSWORD,
    StaffedRestaurant,
    authorization_for,
    staffed_restaurant,
)

API = API_PREFIX
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


async def test_without_sandbox_restaurant_responds_null(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get(SANDBOX_URL, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"restaurant": None, "accounts": []}


async def test_reset_creates_the_sandbox_restaurant_with_sample_data(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    response = await client.post(RESET_URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    restaurant = body["restaurant"]
    assert (restaurant["name"], restaurant["is_active"]) == ("Restaurante de muestra", True)
    # Con una parte al azar: nadie lo ocupa antes de que se cree.
    assert re.fullmatch(r"muestra-[0-9a-f]{8}", restaurant["slug"])
    assert restaurant["staff_count"] == 3
    assert body["accounts"] == [
        {"kind": "owner", "role_label": "Encargado", "full_name": "Encargado de muestra"},
        {"kind": "waiter", "role_label": "Mesero", "full_name": "Mesero de muestra"},
        {"kind": "custom", "role_label": "Cocinero", "full_name": "Cocinero de muestra"},
    ]
    assert (await client.get(SANDBOX_URL, headers=headers)).json() == body

    row = await session.get(RestaurantRow, restaurant["id"])
    assert row is not None
    assert row.is_sandbox
    dishes = await session.execute(
        select(func.count())
        .select_from(MenuItemRow)
        .where(MenuItemRow.restaurant_id == restaurant["id"])
    )
    assert dishes.scalar_one() == 20
    emails = set(
        (
            await session.execute(
                select(UserRow.email).where(UserRow.restaurant_id == restaurant["id"])
            )
        ).scalars()
    )
    assert emails
    assert all(email.endswith("@muestra.resthub.invalid") for email in emails)
    [(kind, _)] = await _activity(client, headers)
    assert kind == "sandbox_reset"


async def test_reset_archives_the_previous_one_and_creates_another(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    first = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]
    old_token = await _preview_token(client, headers, "owner")

    second = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]

    assert second["id"] != first["id"]
    assert second["is_active"] is True
    assert re.fullmatch(r"muestra-[0-9a-f]{8}", second["slug"])
    assert second["slug"] != first["slug"]
    archived = await session.get(RestaurantRow, first["id"])
    assert archived is not None
    await session.refresh(archived)
    assert (archived.is_active, archived.is_sandbox) == (False, True)
    assert re.fullmatch(rf"archivado-{first['id']}-[0-9a-f]{{8}}", archived.slug)
    active = await session.execute(
        select(func.count())
        .select_from(RestaurantRow)
        .where(RestaurantRow.is_sandbox.is_(True), RestaurantRow.is_active.is_(True))
    )
    assert active.scalar_one() == 1
    # Una vista previa del local archivado deja de servir.
    assert (await client.get(f"{API}/auth/me", headers=_bearer(old_token))).status_code == 401
    assert (await _activity(client, headers))[0] == (
        "sandbox_reset",
        f"local de muestra #{second['id']}; el #{first['id']} quedó archivado",
    )


async def test_real_restaurant_with_sandbox_slug_does_not_block_the_preview(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    """Uno creado antes de reservar el prefijo, con el identificador de antes."""
    real = await staffed_restaurant(session, "muestra")

    reset_result = await client.post(RESET_URL, headers=headers)
    other = await client.post(RESET_URL, headers=headers)
    preview = await client.post(PREVIEW_URL, headers=headers, json={"as": "owner"})

    assert (reset_result.status_code, other.status_code, preview.status_code) == (200, 200, 201)
    row = await session.get(RestaurantRow, real.id)
    assert row is not None
    await session.refresh(row)
    assert (row.slug, row.is_active, row.is_sandbox) == ("muestra", True, False)


async def test_creating_a_second_active_sandbox_restaurant_responds_409(
    session: AsyncSession,
) -> None:
    """Dos pedidos que lo crean a la vez: el índice parcial deja uno y el otro es un 409."""
    await staffed_restaurant(session, "muestra-00000001", is_sandbox=True)
    provisioning = ModuleSandboxProvisioningGateway(session, TEST_HASHER, SystemClock())

    with pytest.raises(platform_errors.SandboxAlreadyActiveError) as raised:
        await provisioning.create()

    assert to_http(raised.value).status_code == 409


async def test_platform_list_excludes_sandbox_restaurants(
    client: AsyncClient, headers: dict[str, str], restaurant_a: StaffedRestaurant
) -> None:
    first = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]
    second = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]

    listing = (await client.get(RESTAURANTS_URL, headers=headers)).json()
    search_result = await client.get(RESTAURANTS_URL, headers=headers, params={"search": "muestra"})

    assert [item["id"] for item in listing["items"]] == [restaurant_a.id]
    assert listing["total"] == 1
    assert search_result.json() == {"items": [], "total": 0}
    for sandbox in (first, second):
        detail = await client.get(f"{RESTAURANTS_URL}/{sandbox['id']}", headers=headers)
        edit = await client.patch(
            f"{RESTAURANTS_URL}/{sandbox['id']}", headers=headers, json={"is_active": True}
        )
        assert (detail.status_code, edit.status_code) == (404, 404)


# --- Vista previa -----------------------------------------------------------


async def test_requesting_the_preview_creates_the_restaurant_if_missing_and_gives_a_code(
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
    ("as_", "name", "role", "can", "cannot"),
    [
        ("owner", "Encargado de muestra", "Encargado", Permission.STAFF_MANAGE, None),
        ("waiter", "Mesero de muestra", "Mesero", Permission.ORDERS_TAKE, Permission.STAFF_MANAGE),
    ],
)
async def test_preview_enters_as_the_sandbox_account_with_its_permissions(
    client: AsyncClient,
    headers: dict[str, str],
    as_: str,
    name: str,
    role: str,
    can: Permission,
    cannot: Permission | None,
) -> None:
    issued = (await client.post(PREVIEW_URL, headers=headers, json={"as": as_})).json()

    response = await client.post(EXCHANGE_URL, json={"code": issued["code"]})

    assert response.status_code == 200
    body = response.json()
    assert body["preview"] is True
    assert (body["user"]["full_name"], body["user"]["role_label"]) == (name, role)
    assert body["restaurant"]["name"] == "Restaurante de muestra"
    assert can in body["permissions"]
    if cannot is not None:
        assert cannot not in body["permissions"]

    token = _bearer(body["access_token"])
    me = await client.get(f"{API}/auth/me", headers=token)
    assert me.json()["preview"] is True
    # Opera el local de muestra como cualquier cuenta: sus mesas, su carta.
    tables = await client.get(f"{API}/tables", headers=token)
    assert tables.status_code == 200
    assert len(tables.json()) == 8
    assert (await client.get(f"{API}/restaurant", headers=token)).status_code == 200
    staff = await client.get(f"{API}/staff", headers=token)
    assert staff.status_code == (200 if as_ == "owner" else 403)


async def test_staff_created_in_the_preview_does_not_take_a_real_email(
    client: AsyncClient, headers: dict[str, str], restaurant_a: StaffedRestaurant
) -> None:
    """Las cuentas no se borran: un correo real tomado en el local de muestra se perdería."""
    token = _bearer(await _preview_token(client, headers, "owner"))
    sandbox = (await client.get(SANDBOX_URL, headers=headers)).json()["restaurant"]
    roles = (await client.get(f"{API}/roles", headers=token)).json()
    waiter = next(role for role in roles if role["kind"] == "waiter")
    new_staff = {
        "email": "Carla@Gmail.com",
        "full_name": "Carla Ríos",
        "role_id": waiter["id"],
        "password": VALID_PASSWORD,
    }

    response = await client.post(f"{API}/staff", headers=token, json=new_staff)

    assert response.status_code == 201
    assert response.json()["email"] == f"carla-{sandbox['id']}@muestra.resthub.invalid"
    # El correo real sigue libre para un local de verdad.
    real = await client.post(
        f"{API}/staff",
        headers=authorization_for(restaurant_a.admin),
        json={**new_staff, "role_id": restaurant_a.waiter.role.id},
    )
    assert (real.status_code, real.json()["email"]) == (201, "carla@gmail.com")


async def test_preview_as_another_account_kind_responds_422(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    for body in ({"as": "custom"}, {"as": "admin"}, {}, {"user_id": 1}):
        response = await client.post(PREVIEW_URL, headers=headers, json=body)
        assert response.status_code == 422


async def test_platform_code_works_only_once(client: AsyncClient, headers: dict[str, str]) -> None:
    code = (await client.post(PREVIEW_URL, headers=headers, json={"as": "waiter"})).json()["code"]

    assert (await client.post(EXCHANGE_URL, json={"code": code})).status_code == 200
    assert (await client.post(EXCHANGE_URL, json={"code": code})).status_code == 401


def _rollback_unconfirmed(client: AsyncClient, session: AsyncSession) -> None:
    """La sesión de cada petición deshace lo que no se confirmó antes de responder.

    La de verdad confirma al terminar, cuando la respuesta ya salió: la pestaña
    nueva puede canjear antes. Así se ve qué quedó confirmado a tiempo.
    """

    async def override() -> AsyncIterator[AsyncSession]:
        yield session
        await session.rollback()

    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_session] = override


async def test_code_is_exchanged_right_after_issue_and_marked_used_before_responding(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    _rollback_unconfirmed(client, session)

    issued = await client.post(PREVIEW_URL, headers=headers, json={"as": "owner"})
    exchanged = await client.post(EXCHANGE_URL, json={"code": issued.json()["code"]})

    assert (issued.status_code, exchanged.status_code) == (201, 200)
    used = (await session.execute(select(PreviewCodeRow.used_at))).scalars().all()
    assert len(used) == 1
    assert used[0] is not None
    assert (
        await client.post(EXCHANGE_URL, json={"code": issued.json()["code"]})
    ).status_code == 401


async def test_reset_is_committed_before_responding(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    _rollback_unconfirmed(client, session)

    reset_result = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]

    assert (await client.get(SANDBOX_URL, headers=headers)).json()["restaurant"] == reset_result


async def test_without_active_account_of_that_kind_responds_409(
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


async def test_sandbox_accounts_cannot_sign_in_with_password(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    sandbox = (await client.post(RESET_URL, headers=headers)).json()["restaurant"]
    accounts = (
        await session.execute(
            select(UserRow.email, UserRow.password_hash).where(
                UserRow.restaurant_id == sandbox["id"]
            )
        )
    ).all()
    assert len(accounts) == 3

    for email, password_hash in accounts:
        # El hash es de un valor aleatorio descartado: ninguna contraseña lo reproduce.
        for attempt in ("resthub123", VALID_PASSWORD, ""):
            assert not TEST_HASHER.verify_and_update(attempt, password_hash)[0]
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
async def test_restaurant_token_does_not_reach_the_preview(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    method: str,
    url: str,
    body: dict[str, str] | None,
) -> None:
    response = await client.request(
        method, url, headers=authorization_for(restaurant_a.admin), json=body
    )

    assert response.status_code == 401


async def test_preview_token_does_not_reach_the_preview(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    token = _bearer(await _preview_token(client, headers, "owner"))

    assert (await client.get(SANDBOX_URL, headers=token)).status_code == 401
    assert (await client.post(PREVIEW_URL, headers=token, json={"as": "owner"})).status_code == 401


async def test_no_credential_responds_401(client: AsyncClient) -> None:
    assert (await client.get(SANDBOX_URL)).status_code == 401
    assert (await client.post(PREVIEW_URL, json={"as": "owner"})).status_code == 401


async def test_logging_out_of_a_preview_leaves_no_entry_in_the_restaurant_log(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    await client.post(RESET_URL, headers=headers)
    token = await _preview_token(client, headers, "waiter")

    response = await client.post(f"{API}/auth/logout", headers=_bearer(token))

    assert response.status_code == 204
    signed_out = await session.execute(
        select(func.count())
        .select_from(ActivityRow)
        .where(ActivityRow.kind == ActivityKind.SIGNED_OUT.value)
    )
    assert signed_out.scalar_one() == 0
