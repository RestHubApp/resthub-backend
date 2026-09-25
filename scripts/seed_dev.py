"""Siembra un restaurante y cuentas de prueba en la base de desarrollo.

Existe para poder abrir las pantallas de cada rol sin crear a mano un
restaurante y su personal. Las dos cuentas comparten la contraseña
`DEMO_PASSWORD`.

Es idempotente: lo que ya existe se deja como está. Se niega a correr salvo que
se cumplan dos condiciones a la vez: la base es local y `DEBUG` está activo.
Mirar solo el host no alcanza: un túnel SSH a la base de producción también se
ve como `127.0.0.1`, y sembraría ahí cuentas con una contraseña que está
escrita en este archivo. Un despliegue corre con `DEBUG=false`, así que la
segunda condición lo deja afuera aunque la base parezca local.

Uso:
    uv run python scripts/seed_dev.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.config import get_settings
from resthub.core.database import SessionFactory, engine
from resthub.core.identity import Role
from resthub.core.security import BcryptPasswordHasher
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.entities import User
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain.entities import Restaurant

DEMO_PASSWORD = "resthub123"
DEMO_RESTAURANT = Restaurant(name="Restaurante Demo", slug="restaurante-demo")

# SQLite no tiene host; Postgres local se escribe de cualquiera de estas formas.
LOCAL_HOSTS = {None, "localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True, slots=True)
class DemoAccount:
    email: str
    full_name: str
    role: Role


ACCOUNTS = (
    DemoAccount("admin@resthub.dev", "Encargado Demo", Role.ADMIN),
    DemoAccount("mesero@resthub.dev", "Mesero Demo", Role.WAITER),
)


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


async def _seed_accounts(session: AsyncSession, restaurant_id: int, report: list[str]) -> None:
    password_hash = BcryptPasswordHasher().hash(DEMO_PASSWORD)
    users = SqlAlchemyUserRepository(session)
    for account in ACCOUNTS:
        if await users.get_by_email(account.email) is not None:
            report.append(f"ya existía  {account.email}")
            continue
        await users.add(
            User(
                restaurant_id=restaurant_id,
                email=account.email,
                full_name=account.full_name,
                role=account.role,
                password_hash=password_hash,
            )
        )
        report.append(f"creada      {account.email} ({account.role.label})")


async def seed() -> list[str]:
    report: list[str] = []
    async with SessionFactory() as session:
        restaurant_id = await _seed_restaurant(session, report)
        await _seed_accounts(session, restaurant_id, report)
        await session.commit()
    await engine.dispose()
    return report


def main() -> int:
    settings = get_settings()
    if not settings.debug:
        print("DEBUG está desactivado. Los datos de prueba solo se siembran en desarrollo.")
        return 1
    if not _is_local_database(settings.database_url):
        print("La base configurada no es local. Los datos de prueba no se siembran ahí.")
        return 1
    for line in asyncio.run(seed()):
        print(line)
    print(f"Contraseña de todas las cuentas: {DEMO_PASSWORD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
