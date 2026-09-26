"""El repositorio de restaurantes contra SQLite: los índices únicos dichos como errores."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import SandboxAlreadyActive, SlugAlreadyTaken


async def test_guardar_con_un_identificador_ocupado_es_un_error_de_dominio(
    session: AsyncSession,
) -> None:
    """Archivar cambia el identificador: si otro ya lo tiene, 409 y no un 500."""
    repository = SqlAlchemyRestaurantRepository(session)
    await repository.add(Restaurant(name="Ocupado", slug="ocupado"))
    muestra = await repository.add(
        Restaurant(name="Muestra", slug="muestra-0a1b2c3d", is_sandbox=True)
    )
    await session.commit()

    muestra.archive("ocupado")
    with pytest.raises(SlugAlreadyTaken):
        await repository.save(muestra)


async def test_hay_a_lo_sumo_un_local_de_muestra_vigente(session: AsyncSession) -> None:
    repository = SqlAlchemyRestaurantRepository(session)
    primero = await repository.add(
        Restaurant(name="Muestra", slug="muestra-00000001", is_sandbox=True)
    )
    await session.commit()

    with pytest.raises(SandboxAlreadyActive):
        await repository.add(Restaurant(name="Muestra", slug="muestra-00000002", is_sandbox=True))

    # Archivado el primero, entra otro; y los reales no cuentan.
    primero.archive("archivado-1-00000000")
    await repository.save(primero)
    await repository.add(Restaurant(name="Muestra", slug="muestra-00000003", is_sandbox=True))
    await repository.add(Restaurant(name="Real", slug="real"))
    await repository.add(Restaurant(name="Otro real", slug="otro-real"))
    await session.commit()
