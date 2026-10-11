"""Pruebas del adaptador de persistencia de cuentas.

El repositorio en memoria de las pruebas de casos de uso no ejecuta SQL, así
que el recorte por restaurante, los filtros, el ordenamiento y la restricción
única solo quedan cubiertos aquí, contra una base real.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.accounts.adapters.persistence.directories import SqlAlchemyRestaurantDirectory
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.exceptions import (
    EmailAlreadyRegisteredError,
    UserNotFoundError,
)
from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary
from resthub.modules.accounts.ports.user_repository import UserQuery
from tests.conftest import StaffedRestaurant, build_user


async def test_duplicate_email_conflicts_even_if_from_another_restaurant(
    session: AsyncSession,
    users: SqlAlchemyUserRepository,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    # Se salta a propósito la comprobación previa del caso de uso: esto es lo
    # que pasa cuando dos altas simultáneas la superan las dos.
    with pytest.raises(EmailAlreadyRegisteredError):
        await users.add(build_user(restaurant_b.id, "mesero@local-a.pe", restaurant_b.waiter.role))


async def test_search_never_leaves_the_restaurant(
    users: SqlAlchemyUserRepository,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    page = await users.search(UserQuery(restaurant_id=restaurant_a.id))
    foreign = await users.search(
        UserQuery(restaurant_id=restaurant_a.id, ids=frozenset({restaurant_b.waiter.id or 0}))
    )

    assert {user.restaurant_id for user in page.items} == {restaurant_a.id}
    assert page.total == 2
    assert foreign.total == 0


async def test_read_is_scoped_to_restaurant(
    users: SqlAlchemyUserRepository,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    assert await users.get_in_restaurant(restaurant_a.id, restaurant_a.waiter.id or 0) is not None
    assert await users.get_in_restaurant(restaurant_a.id, restaurant_b.waiter.id or 0) is None


async def test_save_does_not_move_account_to_another_restaurant(
    users: SqlAlchemyUserRepository,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
) -> None:
    moved = restaurant_a.waiter
    moved.restaurant_id = restaurant_b.id

    with pytest.raises(UserNotFoundError):
        await users.save(moved)


async def test_filters_and_ordering(
    session: AsyncSession, users: SqlAlchemyUserRepository, restaurant_a: StaffedRestaurant
) -> None:
    await users.add(
        build_user(
            restaurant_a.id,
            "ada@local-a.pe",
            restaurant_a.waiter.role,
            full_name="Ada Zamora",
            is_active=False,
        )
    )
    await session.commit()

    active = await users.search(UserQuery(restaurant_id=restaurant_a.id, is_active=True))
    waiters = await users.search(
        UserQuery(
            restaurant_id=restaurant_a.id, role_ids=frozenset({restaurant_a.waiter.role.id or 0})
        )
    )
    by_name = await users.search(UserQuery(restaurant_id=restaurant_a.id, ordering="-full_name"))

    assert active.total == 2
    assert waiters.total == 2
    assert [user.full_name for user in by_name.items] == [
        "Rosa Pérez",
        "Luis Torres",
        "Ada Zamora",
    ]


async def test_restaurant_reader(session: AsyncSession, restaurant_a: StaffedRestaurant) -> None:
    directory = SqlAlchemyRestaurantDirectory(session)

    assert await directory.get(restaurant_a.id) == RestaurantSummary(
        id=restaurant_a.id,
        name="Restaurante local-a",
        slug="local-a",
        timezone="America/Lima",
        is_active=True,
    )
    assert await directory.get(9999) is None
