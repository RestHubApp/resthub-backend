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


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://usuario:clave@postgres.railway.internal:5432/railway",
        "postgres://usuario:clave@postgres.railway.internal:5432/railway",
    ],
)
def test_la_url_de_postgres_sin_controlador_pasa_a_asyncpg(url: str) -> None:
    settings = Settings(_env_file=None, database_url=url)

    assert settings.database_url == (
        "postgresql+asyncpg://usuario:clave@postgres.railway.internal:5432/railway"
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://usuario:clave@host:5432/resthub",
        "sqlite+aiosqlite:///./resthub.db",
    ],
)
def test_la_url_con_controlador_queda_como_esta(url: str) -> None:
    assert Settings(_env_file=None, database_url=url).database_url == url


def test_produccion_acepta_la_url_que_entrega_railway() -> None:
    settings = Settings(
        _env_file=None,
        debug=False,
        jwt_secret_key=SECRETO_PROPIO,
        database_url="postgresql://usuario:clave@postgres.railway.internal:5432/railway",
    )
    assert settings.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.parametrize(
    "valor",
    [
        '["https://resthub.example.com", "http://localhost:5173"]',
        "https://resthub.example.com,http://localhost:5173",
        " https://resthub.example.com/ , http://localhost:5173 ,",
    ],
)
def test_cors_se_lee_como_json_o_separado_por_comas(
    valor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", valor)

    assert Settings(_env_file=None).cors_allowed_origins == [
        "https://resthub.example.com",
        "http://localhost:5173",
    ]
