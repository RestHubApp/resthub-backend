"""Siembra un restaurante de prueba completo en la base de desarrollo.

Existe para poder abrir las pantallas de cada rol sin crear a mano un
restaurante, su personal, su carta y su almacén. Siembra el "Restaurante Demo"
con los datos de muestra de `resthub/wiring/sample_restaurant.py` (los mismos
del local de muestra de la vista previa): la carta, las mesas, los insumos, las
recetas, los roles Encargado, Mesero y Cocinero y una cuenta para cada uno.
Además siembra una cuenta de la administración del sistema para entrar a
`/plataforma`. Las cuentas comparten la contraseña `DEMO_PASSWORD`.

Es idempotente: lo que ya existe se deja como está. Se niega a correr salvo que
se cumplan dos condiciones a la vez: la base es local y `DEBUG` está activo.
Mirar solo el host no alcanza: un túnel SSH a la base de producción también se
ve como `127.0.0.1`, y sembraría ahí cuentas con una contraseña que está
escrita en este archivo. Un despliegue corre con `DEBUG=false`, así que la
segunda condición lo deja afuera aunque la base parezca local. El entorno de
demostración desplegado lo habilita a propósito con `ALLOW_DEMO_SEED=true`.

Uso:
    uv run python scripts/seed_dev.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.config import get_settings
from resthub.core.database import SessionFactory, engine
from resthub.core.security import BcryptPasswordHasher
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain.entities import Restaurant
from resthub.wiring.sample_restaurant import sample_accounts, seed_sample_restaurant

DEMO_PASSWORD = "resthub123"
DEMO_RESTAURANT = Restaurant(name="Restaurante Demo", slug="restaurante-demo")

# SQLite no tiene host; Postgres local se escribe de cualquiera de estas formas.
LOCAL_HOSTS = {None, "localhost", "127.0.0.1", "::1"}

ACCOUNTS = sample_accounts(
    emails=("admin@resthub.dev", "mesero@resthub.dev", "cocina@resthub.dev"),
    names=("Encargado Demo", "Mesero Demo", "Cocinero Demo"),
)

# No es del restaurante demo: es de la plataforma, que administra a todos.
PLATFORM_ADMIN_EMAIL = "plataforma@resthub.dev"
PLATFORM_ADMIN_NAME = "Administración RestHub"


def _is_local_database(database_url: str) -> bool:
    return make_url(database_url).host in LOCAL_HOSTS


async def _seed_restaurant(session: AsyncSession, report: list[str]) -> int:
    restaurants = SqlAlchemyRestaurantRepository(session)
    existing = await restaurants.get_by_slug(DEMO_RESTAURANT.slug)
    if existing is not None and existing.id is not None:
        report.append(f"ya existía  restaurante {existing.slug}")
        return existing.id
    created = await restaurants.add(
        Restaurant(name=DEMO_RESTAURANT.name, slug=DEMO_RESTAURANT.slug)
    )
    report.append(f"creado      restaurante {created.slug}")
    return created.id or 0


async def _seed_platform_admin(session: AsyncSession, report: list[str]) -> None:
    admins = SqlAlchemyPlatformAdminRepository(session)
    if await admins.get_by_email(PLATFORM_ADMIN_EMAIL) is not None:
        report.append(f"ya existía  {PLATFORM_ADMIN_EMAIL} (plataforma)")
        return
    await admins.add(
        PlatformAdmin(
            email=PLATFORM_ADMIN_EMAIL,
            full_name=PLATFORM_ADMIN_NAME,
            password_hash=BcryptPasswordHasher().hash(DEMO_PASSWORD),
        )
    )
    report.append(f"creada      {PLATFORM_ADMIN_EMAIL} (plataforma)")


async def seed_into(session: AsyncSession, password_hash: str) -> list[str]:
    """Todo lo del demo en la sesión recibida, sin confirmar.

    Separada de `seed` para que una prueba la corra contra su propia base.
    """
    report: list[str] = []
    restaurant_id = await _seed_restaurant(session, report)
    await seed_sample_restaurant(session, restaurant_id, ACCOUNTS, password_hash, report)
    # La cuenta de plataforma no tiene restaurante: con su contraseña
    # pública, en un entorno demo desplegado cualquiera administraría todos
    # los locales. Solo se siembra en desarrollo local.
    settings = get_settings()
    if settings.debug and _is_local_database(settings.database_url):
        await _seed_platform_admin(session, report)
    else:
        report.append(
            "plataforma  no se siembra fuera de desarrollo local: "
            "usa scripts/create_platform_admin.py"
        )
    return report


async def seed() -> list[str]:
    async with SessionFactory() as session:
        report = await seed_into(session, BcryptPasswordHasher().hash(DEMO_PASSWORD))
        await session.commit()
    await engine.dispose()
    return report


def main() -> int:
    settings = get_settings()
    if settings.allow_demo_seed:
        # Lo pidió quien configuró el entorno: es el de demostración.
        print("ALLOW_DEMO_SEED activo: se siembra aunque la base no sea local.")
    elif not settings.debug:
        print("DEBUG está desactivado. Los datos de prueba solo se siembran en desarrollo.")
        return 1
    elif not _is_local_database(settings.database_url):
        print("La base configurada no es local. Los datos de prueba no se siembran ahí.")
        return 1
    for line in asyncio.run(seed()):
        print(line)
    print(f"Contraseña de todas las cuentas: {DEMO_PASSWORD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
