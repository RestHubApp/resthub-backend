from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from resthub.core.config import get_settings
from resthub.core.db_timing import instrument


class Base(DeclarativeBase):
    """Base declarativa compartida por los modelos de persistencia.

    Vive en el núcleo para que Alembic vea un único metadata, pero ninguna capa
    de dominio la importa: los contratos de Import Linter lo prohíben.
    """


_settings = get_settings()

# `hide_parameters`: sin esto, SQLAlchemy copia los valores de la consulta en el
# mensaje de sus excepciones (`[parameters: ('juan@x.com', '$2b$12$…')]`), y ese
# mensaje termina en los logs y en el panel de observabilidad con correos,
# hashes de contraseña o códigos. La sentencia con sus marcadores sí se muestra.
ENGINE_OPTIONS: dict[str, bool] = {"echo": False, "future": True, "hide_parameters": True}

if _settings.database_url.startswith("sqlite"):
    engine = create_async_engine(_settings.database_url, **ENGINE_OPTIONS)
else:
    # En PostgreSQL remoto (Railway, o cualquiera detrás de un pooler), las
    # conexiones inactivas se cierran en minutos. `pool_pre_ping=True` descarta
    # conexiones muertas antes de usarlas y `statement_cache_size: 0` evita
    # conflictos con prepared statements al pasar por poolers.
    engine = create_async_engine(
        _settings.database_url,
        **ENGINE_OPTIONS,
        pool_pre_ping=True,
        pool_recycle=300,
        # El panel BI pide ocho indicadores a la vez. Con el pool por omisión
        # (5 fijas) las que sobran se abren y se cierran en cada visita, y abrir
        # una conexión cuesta más que la consulta. Diez fijas cubren el panel.
        pool_size=10,
        max_overflow=5,
        connect_args={"statement_cache_size": 0},
    )
instrument(engine)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return SessionFactory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
