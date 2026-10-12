r"""Re-cifra los datos personales con la llave vigente (rotación de `DATA_ENCRYPTION_KEY`).

Pasos para rotar (ver «Cifrado en reposo» del README):
  1. Genera una llave nueva: `uv run python -m resthub.core.data_keys`.
  2. Mueve la llave actual a `DATA_ENCRYPTION_PREVIOUS_KEYS` (separadas por comas si hay
     más de una) y pon la nueva en `DATA_ENCRYPTION_KEY`. Reinicia la aplicación: lee lo
     viejo y escribe con la llave nueva.
  3. Corre este script. Re-cifra cada valor que no use la llave vigente y recalcula el
     índice ciego del teléfono (`customers.phone_key`), que depende de la llave: hasta
     que termine, la búsqueda por teléfono no encuentra a los clientes.
  4. Cuando diga que no queda nada por rotar, quita `DATA_ENCRYPTION_PREVIOUS_KEYS`.

Descubre solo las columnas `EncryptedText` del modelo, así que una columna cifrada nueva
entra sin tocar el script. Es idempotente: se puede repetir o retomar. Nunca imprime
datos ni llaves, solo cuántos valores rotó por tabla.

Uso:
    uv run python scripts/rotate_data_key.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity_log import ActivityRow
from resthub.core.crypto import get_cipher
from resthub.core.database import Base, SessionFactory, engine
from resthub.core.encrypted_types import EncryptedText
from resthub.modules.accounts.adapters.persistence import models as accounts_models
from resthub.modules.billing.adapters.persistence import models as billing_models
from resthub.modules.customers.adapters.persistence import models as customers_models
from resthub.modules.insights.adapters.persistence import models as insights_models
from resthub.modules.inventory.adapters.persistence import models as inventory_models
from resthub.modules.menu.adapters.persistence import models as menu_models
from resthub.modules.orders.adapters.persistence import models as orders_models
from resthub.modules.platform.adapters.persistence import models as platform_models
from resthub.modules.reservations.adapters.persistence import models as reservations_models
from resthub.modules.restaurants.adapters.persistence import models as restaurants_models

# Importados para que `Base.metadata` tenga todas las tablas, como en `alembic/env.py`.
REGISTERED_MODELS = (
    ActivityRow,
    accounts_models,
    billing_models,
    customers_models,
    insights_models,
    inventory_models,
    menu_models,
    orders_models,
    platform_models,
    reservations_models,
    restaurants_models,
)

BATCH = 500
# El índice ciego del teléfono vive en `customers.phone_key`, junto a `customers.phone`.
BLIND_INDEX = {("customers", "phone"): "phone_key"}


def encrypted_columns() -> dict[str, list[str]]:
    """Tabla -> columnas `EncryptedText` del modelo."""
    found: dict[str, list[str]] = {}
    for table in Base.metadata.sorted_tables:
        names = [c.name for c in table.columns if isinstance(c.type, EncryptedText)]
        if names:
            found[table.name] = names
    return found


async def _rotate_table(
    session: AsyncSession, table_name: str, columns: list[str], *, dry_run: bool
) -> int:
    cipher = get_cipher()
    index_columns = [
        BLIND_INDEX[(table_name, c)] for c in columns if (table_name, c) in BLIND_INDEX
    ]
    names = [*columns, *index_columns]
    # Columnas de texto plano: se leen los valores cifrados tal cual están guardados.
    target = sa.table(
        table_name, sa.column("id", sa.Integer), *(sa.column(c, sa.Text) for c in names)
    )
    rotated = 0
    last_id = 0
    while True:
        batch = (
            await session.execute(
                sa.select(target).where(target.c.id > last_id).order_by(target.c.id).limit(BATCH)
            )
        ).all()
        if not batch:
            return rotated
        for row in batch:
            stored = {c: getattr(row, c) or "" for c in columns}
            blind = {
                BLIND_INDEX[(table_name, c)]: c for c in columns if (table_name, c) in BLIND_INDEX
            }
            stale_index = any(
                getattr(row, index)
                != cipher.blind_index(cipher.decrypt(stored[source]).replace(" ", ""))
                for index, source in blind.items()
            )
            if not stale_index and not any(cipher.needs_rotation(v) for v in stored.values()):
                continue
            rotated += 1
            if dry_run:
                continue
            plain = {c: cipher.decrypt(v) for c, v in stored.items()}
            changes = {c: cipher.encrypt(v) for c, v in plain.items()}
            for index, source in blind.items():
                changes[index] = cipher.blind_index(plain[source].replace(" ", ""))
            await session.execute(sa.update(target).where(target.c.id == row.id).values(**changes))
        last_id = batch[-1].id


async def rotate(session: AsyncSession, *, dry_run: bool = False) -> dict[str, int]:
    """Filas rotadas por tabla (o las que se rotarían con `dry_run`)."""
    return {
        table: await _rotate_table(session, table, columns, dry_run=dry_run)
        for table, columns in encrypted_columns().items()
    }


async def _run(*, dry_run: bool) -> dict[str, int]:
    async with SessionFactory() as session:
        result = await rotate(session, dry_run=dry_run)
        await session.commit()
    await engine.dispose()
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Re-cifra los datos con la llave vigente.")
    parser.add_argument("--dry-run", action="store_true", help="Solo cuenta, no escribe")
    arguments = parser.parse_args(argv)
    result = asyncio.run(_run(dry_run=arguments.dry_run))
    verb = "por rotar" if arguments.dry_run else "rotadas"
    for table, count in result.items():
        print(f"{table}: {count} filas {verb}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
