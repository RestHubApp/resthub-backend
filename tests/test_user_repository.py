"""Pruebas del adaptador de persistencia de cuentas.

El repositorio en memoria de las pruebas de casos de uso no ejecuta SQL, así
que el recorte por restaurante, los filtros, el ordenamiento y la restricción
única solo quedan cubiertos aquí, contra una base real.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.identity import Role
from resthub.modules.accounts.adapters.persistence.directories import SqlRestaurantDirectory
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.exceptions import EmailAlreadyRegistered, UserNotFound
from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary
from resthub.modules.accounts.ports.user_repository import UserQuery
from tests.conftest import StaffedRestaurant, build_user


async def test_el_correo_repetido_choca_aunque_sea_de_otro_restaurante(
    session: AsyncSession,
    users: SqlAlchemyUserRepository,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
) -> None:
    # Se salta a propósito la comprobación previa del caso de uso: esto es lo
    # que pasa cuando dos altas simultáneas la superan las dos.
    with pytest.raises(EmailAlreadyRegistered):
        await users.add(build_user(local_b.id, "mesero@local-a.pe"))


async def test_la_busqueda_nunca_sale_del_restaurante(
    users: SqlAlchemyUserRepository, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    page = await users.search(UserQuery(restaurant_id=local_a.id))
    ajenos = await users.search(
        UserQuery(restaurant_id=local_a.id, ids=frozenset({local_b.waiter.id or 0}))
    )

    assert {user.restaurant_id for user in page.items} == {local_a.id}
    assert page.total == 2
    assert ajenos.total == 0


async def test_leer_acotado_al_restaurante(
    users: SqlAlchemyUserRepository, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    assert await users.get_in_restaurant(local_a.id, local_a.waiter.id or 0) is not None
    assert await users.get_in_restaurant(local_a.id, local_b.waiter.id or 0) is None


async def test_guardar_no_muda_una_cuenta_de_restaurante(
    users: SqlAlchemyUserRepository, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    mudado = local_a.waiter
    mudado.restaurant_id = local_b.id

    with pytest.raises(UserNotFound):
        await users.save(mudado)


async def test_filtros_y_orden(
    session: AsyncSession, users: SqlAlchemyUserRepository, local_a: StaffedRestaurant
) -> None:
    await users.add(
        build_user(local_a.id, "ada@local-a.pe", full_name="Ada Zamora", is_active=False)
    )
    await session.commit()

    activos = await users.search(UserQuery(restaurant_id=local_a.id, is_active=True))
    meseros = await users.search(
        UserQuery(restaurant_id=local_a.id, roles=frozenset({Role.WAITER}))
    )
    por_nombre = await users.search(UserQuery(restaurant_id=local_a.id, ordering="-full_name"))

    assert activos.total == 2
    assert meseros.total == 2
    assert [user.full_name for user in por_nombre.items] == [
        "Rosa Pérez",
        "Luis Torres",
        "Ada Zamora",
    ]


async def test_el_lector_de_restaurantes(session: AsyncSession, local_a: StaffedRestaurant) -> None:
    directory = SqlRestaurantDirectory(session)

    assert await directory.get(local_a.id) == RestaurantSummary(
        id=local_a.id, name="Restaurante local-a", slug="local-a", is_active=True
    )
    assert await directory.get(9999) is None
