"""Da de alta un restaurante y a su primer encargado.

No hay registro público: cada restaurante nuevo entra por acá, con acceso
directo a la base. Restaurante y encargado se crean en una sola transacción, así
que un error a mitad de camino no deja un restaurante sin nadie que lo
administre.

Si no se pasa `--password`, la pide por consola sin mostrarla; con `--generate`
genera una y la imprime una sola vez para entregársela al encargado.

Uso:
    uv run python scripts/create_restaurant.py \\
        --name "Cevichería Doña Rosa" --slug dona-rosa \\
        --admin-email rosa@example.com --admin-name "Rosa Pérez"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys
from dataclasses import dataclass

from resthub.core.database import SessionFactory, engine
from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.core.security import BcryptPasswordHasher
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.entities import MIN_PASSWORD_LENGTH
from resthub.modules.accounts.domain.exceptions import AccountsError
from resthub.modules.accounts.use_cases.manage_staff import (
    RegisterFirstAdmin,
    RegisterFirstAdminCommand,
)
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain.exceptions import RestaurantsError
from resthub.modules.restaurants.use_cases.create_restaurant import (
    CreateRestaurant,
    CreateRestaurantCommand,
)

# 18 bytes dan 24 caracteres URL-safe: de sobra por encima del mínimo y todavía
# dictables por teléfono si hace falta.
GENERATED_PASSWORD_BYTES = 18


@dataclass(frozen=True, slots=True)
class Request:
    name: str
    slug: str
    timezone: str
    admin_email: str
    admin_name: str
    password: str


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alta de un restaurante y su primer encargado.")
    parser.add_argument("--name", required=True, help="Nombre visible del restaurante")
    parser.add_argument("--slug", required=True, help="Identificador corto: minúsculas y guiones")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="Zona horaria IANA")
    parser.add_argument("--admin-email", required=True, help="Correo del primer encargado")
    parser.add_argument("--admin-name", required=True, help="Nombre del primer encargado")
    password = parser.add_mutually_exclusive_group()
    password.add_argument("--password", help="Contraseña del encargado (queda en el historial)")
    password.add_argument("--generate", action="store_true", help="Generar una contraseña")
    return parser.parse_args(argv)


def _resolve_password(arguments: argparse.Namespace) -> str:
    if arguments.password:
        return str(arguments.password)
    if arguments.generate:
        return secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)
    first = getpass.getpass(f"Contraseña del encargado (mínimo {MIN_PASSWORD_LENGTH}): ")
    if getpass.getpass("Repítela: ") != first:
        raise ValueError("Las contraseñas no coinciden.")
    return first


async def create(request: Request) -> tuple[int, int]:
    async with SessionFactory() as session:
        restaurant = await CreateRestaurant(SqlAlchemyRestaurantRepository(session))(
            CreateRestaurantCommand(name=request.name, slug=request.slug, timezone=request.timezone)
        )
        admin = await RegisterFirstAdmin(SqlAlchemyUserRepository(session), BcryptPasswordHasher())(
            RegisterFirstAdminCommand(
                restaurant_id=restaurant.id or 0,
                email=request.admin_email,
                full_name=request.admin_name,
                password=request.password,
            )
        )
        await session.commit()
    await engine.dispose()
    return restaurant.id or 0, admin.id or 0


def main(argv: list[str]) -> int:
    arguments = _parse(argv)
    try:
        password = _resolve_password(arguments)
        restaurant_id, admin_id = asyncio.run(
            create(
                Request(
                    name=arguments.name,
                    slug=arguments.slug,
                    timezone=arguments.timezone,
                    admin_email=arguments.admin_email,
                    admin_name=arguments.admin_name,
                    password=password,
                )
            )
        )
    except (ValueError, AccountsError, RestaurantsError) as error:
        print(f"No se creó nada: {error}", file=sys.stderr)
        return 1

    print(f"Restaurante {arguments.slug} creado (id {restaurant_id}).")
    print(f"Encargado {arguments.admin_email} creado (id {admin_id}).")
    if arguments.generate:
        print(f"Contraseña generada, se muestra una sola vez: {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
