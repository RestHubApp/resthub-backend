r"""Activa o desactiva una cuenta de la administración del sistema.

No hay pantalla para gestionar estas cuentas: igual que el alta, entra por acá
con acceso directo a la base. Desactivar una cuenta corta también sus sesiones
abiertas, y reactivarla no las devuelve: tiene que iniciar sesión de nuevo.

Uso:
    uv run python scripts/set_platform_admin_status.py \\
        --email equipo@resthub.pe --deactivate
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from resthub.core.clock import SystemClock
from resthub.core.database import SessionFactory, engine
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.exceptions import PlatformError
from resthub.modules.platform.use_cases.manage_admins import ChangeAdminStatus


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Activa o desactiva una cuenta de plataforma.")
    parser.add_argument("--email", required=True, help="Correo de la cuenta")
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--activate", action="store_true", help="Activar la cuenta")
    state.add_argument("--deactivate", action="store_true", help="Desactivar la cuenta")
    return parser.parse_args(argv)


async def change(email: str, *, is_active: bool) -> int:
    async with SessionFactory() as session:
        admin = await ChangeAdminStatus(SqlAlchemyPlatformAdminRepository(session), SystemClock())(
            email, is_active=is_active
        )
        await session.commit()
    await engine.dispose()
    return admin.id or 0


def main(argv: list[str]) -> int:
    arguments = _parse(argv)
    try:
        admin_id = asyncio.run(change(arguments.email, is_active=arguments.activate))
    except PlatformError as error:
        print(f"No se cambió nada: {error}", file=sys.stderr)
        return 1

    state = "activada" if arguments.activate else "desactivada"
    print(f"Cuenta de plataforma {arguments.email} {state} (id {admin_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
