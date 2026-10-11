"""El repositorio de restaurantes contra SQLite: los índices únicos dichos como errores."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.modules.restaurants.domain.exceptions import (
    SandboxAlreadyActiveError,
    SlugAlreadyTakenError,
)


async def test_save_with_taken_slug_is_a_domain_error(
    session: AsyncSession,
) -> None:
    """Archivar cambia el identificador: si otro ya lo tiene, 409 y no un 500."""
    repository = SqlAlchemyRestaurantRepository(session)
    await repository.add(Restaurant(name="Ocupado", slug="ocupado"))
    sandbox = await repository.add(
        Restaurant(name="Muestra", slug="muestra-0a1b2c3d", is_sandbox=True)
    )
    await session.commit()

    sandbox.archive("ocupado")
    with pytest.raises(SlugAlreadyTakenError):
        await repository.save(sandbox)


async def test_at_most_one_active_sandbox_restaurant(session: AsyncSession) -> None:
    repository = SqlAlchemyRestaurantRepository(session)
    first = await repository.add(
        Restaurant(name="Muestra", slug="muestra-00000001", is_sandbox=True)
    )
    await session.commit()

    with pytest.raises(SandboxAlreadyActiveError):
        await repository.add(Restaurant(name="Muestra", slug="muestra-00000002", is_sandbox=True))

    # Archivado el primero, entra otro; y los reales no cuentan.
    first.archive("archivado-1-00000000")
    await repository.save(first)
    await repository.add(Restaurant(name="Muestra", slug="muestra-00000003", is_sandbox=True))
    await repository.add(Restaurant(name="Real", slug="real"))
    await repository.add(Restaurant(name="Otro real", slug="otro-real"))
    await session.commit()
