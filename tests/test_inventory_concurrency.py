"""El inventario aguanta peticiones simultáneas sobre el mismo insumo o la misma receta.

Cada petición usa su propia sesión de la base, como en producción: con la
sesión compartida de las demás pruebas las peticiones no se solaparían. Solo
PostgreSQL bloquea filas; SQLite en memoria serializa todo por su cuenta.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.database import get_session
from resthub.modules.inventory.adapters.persistence.directories import SqlAlchemyDishDirectory
from resthub.modules.inventory.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyIngredientRepository,
    SqlAlchemyStockLedgerRepository,
)
from resthub.modules.inventory.domain.entities import MovementKind, StockMovement
from tests.builders import seed_menu
from tests.conftest import DATABASE_UNDER_TEST, StaffedRestaurant, authorization_for, url

INVENTORY_URL = url("inventory")

pytestmark = pytest.mark.skipif(
    not DATABASE_UNDER_TEST.is_postgres, reason="los bloqueos de fila son de PostgreSQL"
)


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


async def _purchase(
    client: AsyncClient, restaurant: StaffedRestaurant, ingredient_id: int, **body: str
) -> int:
    response = await client.post(
        f"{INVENTORY_URL}/purchases",
        json={"ingredient_id": ingredient_id, **body},
        headers=authorization_for(restaurant.admin),
    )
    return response.status_code


async def _ingredient(client: AsyncClient, restaurant: StaffedRestaurant) -> dict[str, Any]:
    response = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": "Arroz", "unit": "g", "unit_cost": "0.004"},
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.usefixtures("per_request_sessions")
async def test_simultaneous_purchases_weight_the_cost_one_after_the_other(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    rice = await _ingredient(client, restaurant_a)
    first = await _purchase(client, restaurant_a, rice["id"], quantity="5000", unit_cost="0.004")
    assert first == 201

    codes = await asyncio.gather(
        _purchase(client, restaurant_a, rice["id"], quantity="10000", unit_cost="0.006"),
        _purchase(client, restaurant_a, rice["id"], quantity="10000", unit_cost="0.008"),
    )
    level = await client.get(
        f"{INVENTORY_URL}/ingredients/{rice['id']}", headers=authorization_for(restaurant_a.admin)
    )

    assert codes == [201, 201]
    # En serie: (5000·0.004 + 10000·0.006 + 10000·0.008) / 25000, en cualquier orden.
    assert (level.json()["unit_cost"], level.json()["stock"]) == ("0.006400", "25000.000")


@pytest.mark.usefixtures("per_request_sessions")
async def test_simultaneous_saves_of_the_same_recipe_both_succeed(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    menu = await seed_menu(session, restaurant_a.id)
    rice = await _ingredient(client, restaurant_a)
    recipe_url = f"{INVENTORY_URL}/recipes/{menu.lomo}"
    body = {"lines": [{"ingredient_id": rice["id"], "quantity": "150"}]}

    # Un token por petición: cada uno abre su propia sesión de dispositivo.
    responses = await asyncio.gather(
        *(
            client.put(recipe_url, json=body, headers=authorization_for(restaurant_a.admin))
            for _ in range(4)
        )
    )
    recipe = await client.get(recipe_url, headers=authorization_for(restaurant_a.admin))

    assert [response.status_code for response in responses] == [200] * 4
    assert [line["ingredient_id"] for line in recipe.json()["lines"]] == [rice["id"]]


async def _within[T](seconds: float, step: Awaitable[T]) -> T | None:
    """Lo que da `step` antes del plazo; con un bloqueo de más, se queda esperando."""
    try:
        return await asyncio.wait_for(step, seconds)
    except TimeoutError:
        return None


@pytest.mark.usefixtures("per_request_sessions")
async def test_locked_ingredient_still_takes_movements_from_other_requests(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    rice = await _ingredient(client, restaurant_a)
    factory = async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)

    async with factory() as purchase, factory() as kitchen:
        # Una compra tiene el insumo tomado mientras pondera su costo...
        await SqlAlchemyIngredientRepository(purchase).get(
            restaurant_a.id, rice["id"], for_update=True
        )
        # ...y la cocina descuenta el mismo insumo al servir un plato: la clave
        # foránea del movimiento no debe esperar a que la compra termine.
        consumed = await _within(
            3,
            SqlAlchemyStockLedgerRepository(kitchen).add(
                StockMovement(
                    restaurant_id=restaurant_a.id,
                    ingredient_id=rice["id"],
                    kind=MovementKind.ADJUSTMENT,
                    quantity=Decimal(-150),
                    created_by=restaurant_a.admin.id,
                    reason="Consumo",
                )
            ),
        )
        await kitchen.rollback()
        await purchase.rollback()

    assert consumed is not None


@pytest.mark.usefixtures("per_request_sessions")
async def test_dish_locked_by_a_recipe_save_can_still_be_ordered(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    menu = await seed_menu(session, restaurant_a.id)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)

    async with factory() as recipe_save:
        # Un guardado de la receta tiene el plato tomado...
        await SqlAlchemyDishDirectory(recipe_save).get(restaurant_a.id, menu.lomo, for_update=True)
        # ...y el mesero lo pide: la clave foránea del ítem no debe esperarlo.
        ordered = await _within(
            3,
            client.post(
                url("orders"),
                json={"type": "takeaway", "items": [{"menu_item_id": menu.lomo}]},
                headers=authorization_for(restaurant_a.waiter),
            ),
        )
        await recipe_save.rollback()

    assert ordered is not None
    assert ordered.status_code == 201, ordered.text
