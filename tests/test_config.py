"""Pruebas de la configuración y validadores del entorno."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from resthub import main
from resthub.core.config import INSECURE_DEFAULT_SECRET, Settings
from resthub.core.data_keys import generate_key
from tests.conftest import url

OWN_SECRET = "x" * 40
OWN_KEY = generate_key()


def test_production_rejects_sqlite() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL apunta a SQLite en producción"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret_key=OWN_SECRET,
            data_encryption_key=OWN_KEY,
            database_url="sqlite+aiosqlite:///./resthub.db",
        )


def test_production_rejects_development_jwt_secret() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET_KEY conserva el valor de desarrollo"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret_key=INSECURE_DEFAULT_SECRET,
            database_url="postgresql+asyncpg://usuario:clave@host:5432/resthub",
        )


def test_short_secret_is_always_rejected() -> None:
    with pytest.raises(ValueError, match="al menos 32 bytes"):
        Settings(_env_file=None, debug=True, jwt_secret_key="corto")


def test_production_accepts_postgres_and_own_secret() -> None:
    settings = Settings(
        _env_file=None,
        debug=False,
        jwt_secret_key=OWN_SECRET,
        data_encryption_key=OWN_KEY,
        database_url="postgresql+asyncpg://usuario:clave@host:5432/resthub",
    )
    assert not settings.debug
    assert settings.database_url.startswith("postgresql")


def test_development_allows_sqlite_and_default_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # El job de PostgreSQL del CI exporta `DATABASE_URL`; aquí se prueba el valor por omisión.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None, debug=True)
    assert settings.debug
    assert settings.database_url == "sqlite+aiosqlite:///./resthub.db"
    assert settings.app_name == "resthub-api"
    assert settings.jwt_secret_key == INSECURE_DEFAULT_SECRET
    # Los seeds no se habilitan fuera de lo local si nadie lo pide.
    assert settings.allow_demo_seed is False


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://usuario:clave@postgres.railway.internal:5432/railway",
        "postgres://usuario:clave@postgres.railway.internal:5432/railway",
    ],
)
def test_postgres_url_without_driver_becomes_asyncpg(url: str) -> None:
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
def test_url_with_driver_is_left_as_is(url: str) -> None:
    assert Settings(_env_file=None, database_url=url).database_url == url


def test_production_accepts_the_url_railway_provides() -> None:
    settings = Settings(
        _env_file=None,
        debug=False,
        jwt_secret_key=OWN_SECRET,
        data_encryption_key=OWN_KEY,
        database_url="postgresql://usuario:clave@postgres.railway.internal:5432/railway",
    )
    assert settings.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.parametrize(
    "value",
    [
        '["https://resthub.example.com", "http://localhost:5173"]',
        "https://resthub.example.com,http://localhost:5173",
        " https://resthub.example.com/ , http://localhost:5173 ,",
    ],
)
def test_cors_is_read_as_json_or_comma_separated(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", value)

    assert Settings(_env_file=None).cors_allowed_origins == [
        "https://resthub.example.com",
        "http://localhost:5173",
    ]


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_cors_pattern_is_not_used(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGIN_REGEX", value)

    assert Settings(_env_file=None).cors_allowed_origin_regex is None


def test_invalid_cors_pattern_is_rejected() -> None:
    with pytest.raises(ValueError, match="CORS_ALLOWED_ORIGIN_REGEX no es una expresión válida"):
        Settings(_env_file=None, cors_allowed_origin_regex="https://(sin-cerrar")


PREVIEWS_PATTERN = r"https://resthub-frontend-[a-z0-9-]+-equipo\.vercel\.app"


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("http://localhost:5173", True),
        ("https://resthub-frontend-git-develop-equipo.vercel.app", True),
        ("https://resthub-frontend-a1b2c3-equipo.vercel.app", True),
        ("https://resthub-frontend-a1b2c3-equipo.vercel.app.otro.com", False),
        ("https://otro-equipo.vercel.app", False),
    ],
)
async def test_cors_accepts_origins_matching_the_pattern(
    origin: str, allowed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        main,
        "settings",
        main.settings.model_copy(
            update={
                "cors_allowed_origins": ["http://localhost:5173"],
                "cors_allowed_origin_regex": PREVIEWS_PATTERN,
            }
        ),
    )
    app = main.create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.options(
            url("health"),
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )

    assert (response.headers.get("access-control-allow-origin") == origin) is allowed
