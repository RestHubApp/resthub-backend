"""Da de alta una cuenta de la administración del sistema.

No hay registro público: la primera cuenta de plataforma, y las que sigan,
entran por acá con acceso directo a la base. Con esa cuenta se dan de alta los
restaurantes desde `/plataforma`.

La contraseña nunca va en la línea de comandos, donde quedaría en el historial.
Sale, en este orden, de:

- `--password-stdin`: la primera línea de la entrada estándar
  (`printf '%s' "$CLAVE" | … --password-stdin`);
- `--generate`: se genera una y se imprime una sola vez;
- la variable de entorno `PLATFORM_ADMIN_PASSWORD`;
- si no, se pide por consola sin mostrarla.

Uso:
    uv run python scripts/create_platform_admin.py \\
        --email equipo@resthub.pe --name "Equipo RestHub"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import sys

from resthub.core.credentials import MIN_PASSWORD_LENGTH
from resthub.core.database import SessionFactory, engine
from resthub.core.security import BcryptPasswordHasher
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.exceptions import PlatformError
from resthub.modules.platform.use_cases.manage_admins import RegisterAdmin, RegisterAdminCommand

PASSWORD_VARIABLE = "PLATFORM_ADMIN_PASSWORD"
# Como en `create_restaurant.py`: 24 caracteres URL-safe.
GENERATED_PASSWORD_BYTES = 18


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alta de una cuenta de plataforma.")
    parser.add_argument("--email", required=True, help="Correo de la cuenta")
    parser.add_argument("--name", required=True, help="Nombre visible")
    password = parser.add_mutually_exclusive_group()
    password.add_argument(
        "--password-stdin", action="store_true", help="Leer la contraseña de la entrada estándar"
    )
    password.add_argument("--generate", action="store_true", help="Generar una contraseña")
    return parser.parse_args(argv)


def _resolve_password(arguments: argparse.Namespace) -> str:
    if arguments.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    if arguments.generate:
        return secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)
    from_environment = os.environ.get(PASSWORD_VARIABLE)
    if from_environment:
        return from_environment
    first = getpass.getpass(f"Contraseña (mínimo {MIN_PASSWORD_LENGTH}): ")
    if getpass.getpass("Repítela: ") != first:
        raise ValueError("Las contraseñas no coinciden.")
    return first


async def create(email: str, name: str, password: str) -> tuple[int, str]:
    async with SessionFactory() as session:
        admin = await RegisterAdmin(
            SqlAlchemyPlatformAdminRepository(session), BcryptPasswordHasher()
        )(RegisterAdminCommand(email=email, full_name=name, password=password))
        await session.commit()
    await engine.dispose()
    return admin.id or 0, admin.email


def main(argv: list[str]) -> int:
    arguments = _parse(argv)
    try:
        password = _resolve_password(arguments)
        admin_id, email = asyncio.run(create(arguments.email, arguments.name, password))
    except (ValueError, PlatformError) as error:
        print(f"No se creó nada: {error}", file=sys.stderr)
        return 1

    print(f"Cuenta de plataforma {email} creada (id {admin_id}).")
    if arguments.generate:
        print(f"Contraseña generada, se muestra una sola vez: {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
