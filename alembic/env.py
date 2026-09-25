"""Entorno de migraciones.

Importa los modelos de persistencia de cada módulo para que `Base.metadata`
esté completo antes de comparar contra la base. Un módulo nuevo cuyo modelo no
se importe aquí quedaría fuera de las migraciones sin dar error.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from resthub.core.activity_log import ActivityRow
from resthub.core.config import get_settings
from resthub.core.database import Base
from resthub.modules.accounts.adapters.persistence import models as accounts_models
from resthub.modules.insights.adapters.persistence import models as insights_models
from resthub.modules.inventory.adapters.persistence import models as inventory_models
from resthub.modules.menu.adapters.persistence import models as menu_models
from resthub.modules.orders.adapters.persistence import models as orders_models
from resthub.modules.restaurants.adapters.persistence import models as restaurants_models

# Los modelos se importan para que sus tablas queden registradas en
# `Base.metadata`. La tupla existe para que la intención sea explícita: sin
# ella son importaciones aparentemente sin uso, y hacen falta todas.
REGISTERED_MODELS = (
    ActivityRow,
    accounts_models,
    insights_models,
    inventory_models,
    menu_models,
    orders_models,
    restaurants_models,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite no sabe alterar columnas: sin esto, una migración que corre en
        # PostgreSQL falla al reproducirla contra la base local de respaldo.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
