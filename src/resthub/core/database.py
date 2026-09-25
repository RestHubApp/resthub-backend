from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from resthub.core.config import get_settings


class Base(DeclarativeBase):
    """Base declarativa compartida por los modelos de persistencia.

    Vive en el núcleo para que Alembic vea un único metadata, pero ninguna capa
    de dominio la importa: los contratos de Import Linter lo prohíben.
    """


_settings = get_settings()

if _settings.database_url.startswith("sqlite"):
    engine = create_async_engine(_settings.database_url, echo=False, future=True)
else:
    # En PostgreSQL remoto (Railway, o cualquiera detrás de un pooler), las
    # conexiones inactivas se cierran en minutos. `pool_pre_ping=True` descarta
    # conexiones muertas antes de usarlas y `statement_cache_size: 0` evita
    # conflictos con prepared statements al pasar por poolers.
    engine = create_async_engine(
        _settings.database_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"statement_cache_size": 0},
    )
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
