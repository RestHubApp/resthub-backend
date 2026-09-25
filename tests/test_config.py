"""Pruebas de la configuración y validadores del entorno."""

from __future__ import annotations

import pytest

from resthub.core.config import INSECURE_DEFAULT_SECRET, Settings

SECRETO_PROPIO = "una-clave-secreta-suficientemente-larga-de-32-caracteres"


def test_produccion_rechaza_sqlite() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL apunta a SQLite en producción"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret_key=SECRETO_PROPIO,
            database_url="sqlite+aiosqlite:///./resthub.db",
        )


def test_produccion_rechaza_jwt_secret_de_desarrollo() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET_KEY conserva el valor de desarrollo"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret_key=INSECURE_DEFAULT_SECRET,
            database_url="postgresql+asyncpg://usuario:clave@host:5432/resthub",
        )


def test_un_secreto_corto_se_rechaza_siempre() -> None:
    with pytest.raises(ValueError, match="al menos 32 bytes"):
        Settings(_env_file=None, debug=True, jwt_secret_key="corto")


def test_produccion_acepta_postgres_y_secret_propio() -> None:
    settings = Settings(
        _env_file=None,
        debug=False,
        jwt_secret_key=SECRETO_PROPIO,
        database_url="postgresql+asyncpg://usuario:clave@host:5432/resthub",
    )
    assert not settings.debug
    assert settings.database_url.startswith("postgresql")


def test_desarrollo_permite_sqlite_y_secret_por_defecto() -> None:
    settings = Settings(_env_file=None, debug=True)
    assert settings.debug
    assert settings.database_url == "sqlite+aiosqlite:///./resthub.db"
    assert settings.app_name == "resthub-api"
    assert settings.jwt_secret_key == INSECURE_DEFAULT_SECRET
