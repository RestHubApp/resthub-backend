"""TLS hacia PostgreSQL (`DATABASE_SSL_MODE`, ASVS V12.3.1)."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from resthub.core.config import Settings
from resthub.core.data_keys import generate_key
from resthub.core.database import database_connect_args, warn_if_transport_unprotected
from resthub.core.realtime_broker import create_broker

POSTGRES_URL = "postgresql+asyncpg://usuario:clave@host:5432/resthub"


def _production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "debug": False,
        "jwt_secret_key": "x" * 40,
        "data_encryption_key": generate_key(),
        "database_url": POSTGRES_URL,
    }
    return Settings(**(values | overrides))


def test_database_ssl_mode_defaults_to_prefer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_SSL_MODE", raising=False)

    settings = Settings(_env_file=None, debug=True)

    assert settings.database_ssl_mode == "prefer"
    assert database_connect_args(settings) == {"ssl": "prefer"}


@pytest.mark.parametrize("mode", ["disable", "prefer", "require"])
def test_plain_modes_are_passed_to_asyncpg(mode: str) -> None:
    settings = Settings(_env_file=None, debug=True, database_ssl_mode=mode)

    assert database_connect_args(settings) == {"ssl": mode}


def test_verify_full_checks_certificate_and_hostname() -> None:
    settings = Settings(_env_file=None, debug=True, database_ssl_mode="verify-full")

    context = database_connect_args(settings)["ssl"]

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_verify_ca_checks_certificate_but_not_hostname() -> None:
    settings = Settings(_env_file=None, debug=True, database_ssl_mode="verify-ca")

    context = database_connect_args(settings)["ssl"]

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is False


def test_verify_uses_the_configured_root_certificate(tmp_path: Path) -> None:
    missing = tmp_path / "no-existe.pem"
    settings = Settings(
        _env_file=None,
        debug=True,
        database_ssl_mode="verify-full",
        database_ssl_root_cert=str(missing),
    )

    with pytest.raises(FileNotFoundError):
        database_connect_args(settings)


def test_unknown_ssl_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="database_ssl_mode"):
        Settings(_env_file=None, debug=True, database_ssl_mode="allow-everything")


@pytest.mark.parametrize("mode", ["disable", "prefer"])
def test_production_without_enforced_tls_warns_at_startup(mode: str) -> None:
    settings = _production(database_ssl_mode=mode)

    with capture_logs() as logs:
        warn_if_transport_unprotected(settings)

    assert settings.database_transport_unprotected
    assert [entry["event"] for entry in logs] == ["database.tls_not_enforced"]
    assert logs[0]["log_level"] == "warning"
    assert logs[0]["ssl_mode"] == mode


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_production_with_enforced_tls_does_not_warn(mode: str) -> None:
    settings = _production(database_ssl_mode=mode)

    with capture_logs() as logs:
        warn_if_transport_unprotected(settings)

    assert not settings.database_transport_unprotected
    assert logs == []


def test_development_and_sqlite_do_not_warn() -> None:
    development = Settings(
        _env_file=None, debug=True, database_url=POSTGRES_URL, database_ssl_mode="disable"
    )
    sqlite = Settings(_env_file=None, debug=True, database_ssl_mode="disable")

    assert not development.database_transport_unprotected
    assert not sqlite.database_transport_unprotected


class _ListenConnection:
    async def add_listener(self, *_: object) -> None:
        return None

    def add_termination_listener(self, *_: object) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.parametrize("mode", ["verify-full", "require"])
async def test_realtime_broker_uses_database_tls_settings(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: dict[str, Any] = {}

    async def fake_connect(dsn: str, **kwargs: Any) -> _ListenConnection:
        received.update(kwargs, dsn=dsn)
        return _ListenConnection()

    monkeypatch.setattr("resthub.core.realtime_broker.asyncpg.connect", fake_connect)
    broker = create_broker(_production(database_ssl_mode=mode))

    await broker.start()
    await broker.stop()

    assert received["dsn"] == "postgresql://usuario:clave@host:5432/resthub"
    if mode == "verify-full":
        assert isinstance(received["ssl"], ssl.SSLContext)
        assert received["ssl"].check_hostname is True
    else:
        assert received["ssl"] == "require"
