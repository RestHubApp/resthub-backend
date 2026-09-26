"""La semilla de desarrollo sigue sembrando el demo, ahora con los datos de muestra compartidos."""

from __future__ import annotations

from httpx import AsyncClient
from scripts.seed_dev import DEMO_PASSWORD, seed_into
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.accounts.adapters.persistence.models import RoleRow, UserRow
from resthub.modules.inventory.adapters.persistence.models import StockMovementRow
from resthub.modules.menu.adapters.persistence.models import MenuItemRow
from resthub.modules.orders.adapters.persistence.models import DiningTableRow
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow
from tests.conftest import TEST_HASHER


async def _count(session: AsyncSession, model: type) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_siembra_el_demo_y_es_idempotente(session: AsyncSession) -> None:
    password_hash = TEST_HASHER.hash(DEMO_PASSWORD)

    primera = await seed_into(session, password_hash)
    await session.commit()
    segunda = await seed_into(session, password_hash)
    await session.commit()

    assert "creado      restaurante restaurante-demo" in primera
    assert "ya existía  restaurante restaurante-demo" in segunda
    restaurant = (await session.execute(select(RestaurantRow))).scalar_one()
    assert (restaurant.name, restaurant.is_sandbox) == ("Restaurante Demo", False)
    emails = set((await session.execute(select(UserRow.email))).scalars())
    assert emails == {"admin@resthub.dev", "mesero@resthub.dev", "cocina@resthub.dev"}
    roles = set((await session.execute(select(RoleRow.name))).scalars())
    assert roles == {"Encargado", "Mesero", "Cocinero"}
    assert await _count(session, MenuItemRow) == 20
    assert await _count(session, DiningTableRow) == 8
    # El stock inicial entra una sola vez, aunque se corra dos veces.
    assert await _count(session, StockMovementRow) == 29


async def test_las_cuentas_del_demo_entran_con_la_contrasena_conocida(
    client: AsyncClient, session: AsyncSession
) -> None:
    await seed_into(session, TEST_HASHER.hash(DEMO_PASSWORD))
    await session.commit()

    response = await client.post(
        "/api/v1/auth/login", json={"email": "admin@resthub.dev", "password": DEMO_PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["restaurant"]["name"] == "Restaurante Demo"
    assert response.json()["user"]["role_label"] == "Encargado"
