r"""Cierra todas las sesiones abiertas de un restaurante o de todo el sistema (ASVS V7.4.5).

Para un incidente: una clave filtrada, un equipo del local robado, alguien que se
fue con el celular de caja. Cada token vale solo mientras su sesión
(`account_sessions`) siga abierta, así que cerrarlas corta el acceso en la
petición siguiente; cada cuenta vuelve a entrar con su contraseña (y su segundo
paso). No cambia contraseñas ni desactiva cuentas.

Uso:
    uv run python scripts/revoke_all_sessions.py --restaurant mi-restaurante
    uv run python scripts/revoke_all_sessions.py --everyone  # también plataforma y vistas previas
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.account_sessions import SqlSessionRegistry
from resthub.core.clock import Clock, SystemClock
from resthub.core.database import SessionFactory, engine
from resthub.core.geo import NoGeoLocator
from resthub.modules.restaurants.adapters.persistence.models import RestaurantRow


class UnknownRestaurantError(Exception):
    """No hay un restaurante con ese identificador corto."""


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cierra todas las sesiones abiertas.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--restaurant", help="Identificador corto (slug) del restaurante")
    target.add_argument("--everyone", action="store_true", help="Todas las cuentas del sistema")
    return parser.parse_args(argv)


async def revoke_sessions(session: AsyncSession, clock: Clock, *, slug: str | None) -> int:
    """Cierra las sesiones del local `slug` (o todas, con `None`); devuelve cuántas."""
    restaurant_id: int | None = None
    if slug is not None:
        restaurant_id = await session.scalar(
            select(RestaurantRow.id).where(RestaurantRow.slug == slug)
        )
        if restaurant_id is None:
            message = f"No hay un restaurante «{slug}»."
            raise UnknownRestaurantError(message)
    # La vida de un token no importa acá: se cierran todas, vigentes o no.
    registry = SqlSessionRegistry(clock=clock, geo=NoGeoLocator(), token_ttl=timedelta(0))
    closed = await registry.revoke_every(session, restaurant_id=restaurant_id)
    await session.commit()
    return closed


async def _run(slug: str | None) -> int:
    async with SessionFactory() as session:
        closed = await revoke_sessions(session, SystemClock(), slug=slug)
    await engine.dispose()
    return closed


def main(argv: list[str]) -> int:
    arguments = _parse(argv)
    slug = None if arguments.everyone else arguments.restaurant
    try:
        closed = asyncio.run(_run(slug))
    except UnknownRestaurantError as error:
        print(f"No se cerró nada: {error}", file=sys.stderr)
        return 1
    scope = "de todo el sistema" if slug is None else f"del restaurante {slug}"
    print(f"Sesiones cerradas {scope}: {closed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
