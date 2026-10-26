"""Cifrado en reposo de datos personales: núcleo, configuración, columnas y migración."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from httpx import AsyncClient
from scripts.rotate_data_key import encrypted_columns, rotate
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.config import Settings, get_settings
from resthub.core.crypto import DataCipher, email_index, get_cipher
from resthub.core.data_keys import DataEncryptionError, generate_key, parse_key
from resthub.core.two_factor import SecondFactor, SqlTwoFactorStore, current_step, totp
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.customers.adapters.persistence.models import CustomerRow
from tests.conftest import StaffedRestaurant, authorization_for, staffed_restaurant, url
from tests.fakes import FixedClock
from tests.test_migrations import _config

OWN_SECRET = "x" * 40
POSTGRES_URL = "postgresql+asyncpg://usuario:clave@host:5432/resthub"
PHONE = "987 654 321"


def _cipher(*, previous: tuple[str, ...] = ()) -> tuple[DataCipher, str]:
    key = generate_key()
    return DataCipher(parse_key(key), [parse_key(old) for old in previous]), key


def test_cipher_round_trips_with_a_new_nonce_each_time() -> None:
    cipher, _ = _cipher()

    first, second = cipher.encrypt("Jr. Pizarro 450"), cipher.encrypt("Jr. Pizarro 450")

    assert first != second
    assert "Pizarro" not in first
    assert cipher.decrypt(first) == cipher.decrypt(second) == "Jr. Pizarro 450"


def test_cipher_keeps_empty_text_and_reads_rows_from_before_encryption() -> None:
    cipher, _ = _cipher()

    assert cipher.encrypt("") == ""
    assert cipher.decrypt("") == ""
    assert cipher.decrypt("texto antiguo") == "texto antiguo"


def test_cipher_reads_values_of_a_previous_key_and_flags_them_for_rotation() -> None:
    old, old_key = _cipher()
    legacy = old.encrypt("987654321")
    new, _ = _cipher(previous=(old_key,))

    assert new.decrypt(legacy) == "987654321"
    assert new.needs_rotation(legacy)
    assert new.needs_rotation("en claro")
    assert not new.needs_rotation(new.encrypt("987654321"))
    assert not new.needs_rotation("")


def test_cipher_errors_never_include_the_data_or_the_key() -> None:
    cipher, key = _cipher()
    other, _ = _cipher()
    sealed = cipher.encrypt("secreto-del-cliente")
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")

    for bad in (sealed, tampered, "enc1:zzzzzzzz:abc", "enc1:roto"):
        with pytest.raises(DataEncryptionError) as error:
            other.decrypt(bad)
        message = str(error.value)
        assert "secreto-del-cliente" not in message
        assert key not in message
    with pytest.raises(DataEncryptionError):
        cipher.decrypt(tampered)


def test_blind_index_is_stable_per_key_and_hides_the_value() -> None:
    cipher, _ = _cipher()
    other, _ = _cipher()

    assert cipher.blind_index("987654321") == cipher.blind_index("987654321")
    assert cipher.blind_index("987654321") != cipher.blind_index("987654322")
    assert cipher.blind_index("987654321") != other.blind_index("987654321")
    assert "987654321" not in cipher.blind_index("987654321")
    assert cipher.blind_index("") == ""


@pytest.mark.parametrize("bad", ["no-es-base64!", base64.b64encode(b"corta").decode()])
def test_malformed_keys_are_rejected_without_echoing_them(bad: str) -> None:
    with pytest.raises(ValueError, match="inválida") as error:
        Settings(_env_file=None, debug=True, data_encryption_key=bad)

    assert bad not in str(error.value)


def test_production_requires_its_own_encryption_key() -> None:
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "debug": False,
        "jwt_secret_key": OWN_SECRET,
        "database_url": POSTGRES_URL,
    }

    with pytest.raises(ValueError, match="DATA_ENCRYPTION_KEY falta"):
        Settings(**kwargs)
    assert Settings(**kwargs, data_encryption_key=generate_key()).debug is False


def test_development_works_without_a_key() -> None:
    assert Settings(_env_file=None, debug=True).data_encryption_key.get_secret_value() == ""
    assert get_cipher().decrypt(get_cipher().encrypt("hola")) == "hola"


def test_settings_do_not_print_the_key() -> None:
    key = generate_key()

    assert key not in repr(Settings(_env_file=None, debug=True, data_encryption_key=key))


async def _raw(session: AsyncSession, statement: str) -> list[Any]:
    return list((await session.execute(text(statement))).all())


async def test_customer_columns_are_ciphertext_in_the_database_but_clear_in_the_api(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.waiter)
    created = await client.post(
        url("customers"),
        json={
            "name": "Ana Torres",
            "phone": PHONE,
            "email": "ana@correo.pe",
            "address": "Jr. Pizarro 450",
            "reference": "Frente al parque",
            "notes": "Alérgica al maní",
            "consent": True,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text

    stored = (
        await _raw(
            session,
            "SELECT name, phone, phone_key, email, address, reference, notes FROM customers",
        )
    )[0]
    assert stored.name == "Ana Torres"
    for value in (stored.phone, stored.email, stored.address, stored.reference, stored.notes):
        assert value.startswith("enc1:")
    assert "987654321" not in stored.phone_key
    assert len(stored.phone_key) == 64

    card = await client.get(f"{url('customers')}/{created.json()['id']}", headers=headers)
    assert card.json()["phone"] == PHONE
    assert card.json()["notes"] == "Alérgica al maní"

    by_phone = await client.get(url("customers"), params={"q": "987654321"}, headers=headers)
    by_name = await client.get(url("customers"), params={"q": "torres"}, headers=headers)
    partial = await client.get(url("customers"), params={"q": "9876"}, headers=headers)
    assert [c["name"] for c in by_phone.json()["items"]] == ["Ana Torres"]
    assert [c["name"] for c in by_name.json()["items"]] == ["Ana Torres"]
    assert partial.json()["items"] == []


@pytest.fixture
def legacy_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    database = tmp_path / "anterior.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    command.upgrade(_config(), "0025")
    yield database
    get_settings.cache_clear()


def test_migration_encrypts_existing_data_and_can_be_undone(legacy_database: Path) -> None:
    engine = create_engine(f"sqlite:///{legacy_database.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO restaurants (id, name, slug, is_active, timezone, "
                    "max_waiter_discount_percent, auto_out_of_stock, created_at) "
                    "VALUES (1, 'Local', 'local', 1, 'America/Lima', 10, 1, '2026-10-01 20:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO customers (id, restaurant_id, name, phone, phone_key, email, "
                    "address, reference, notes, created_at, consent_version) VALUES "
                    "(1, 1, 'Ana', :phone, '987654321', 'ana@correo.pe', 'Jr. Pizarro 450', "
                    "'Frente al parque', 'Sin cebolla', '2026-10-01 20:00:00', ''), "
                    "(2, 1, 'Beto', '', '', '', '', '', '', '2026-10-01 20:00:00', '')"
                ),
                {"phone": PHONE},
            )

        command.upgrade(_config(), "head")

        with engine.connect() as conn:
            ana = conn.execute(text("SELECT * FROM customers WHERE id = 1")).one()
            beto = conn.execute(text("SELECT * FROM customers WHERE id = 2")).one()
        assert ana.phone.startswith("enc1:")
        assert ana.notes.startswith("enc1:")
        assert get_cipher().decrypt(ana.phone) == PHONE
        assert ana.phone_key == get_cipher().blind_index("987654321")
        assert (beto.phone, beto.phone_key, beto.notes) == ("", "", "")

        command.downgrade(_config(), "0025")

        with engine.connect() as conn:
            back = conn.execute(text("SELECT * FROM customers WHERE id = 1")).one()
        assert (back.phone, back.phone_key, back.email) == (PHONE, "987654321", "ana@correo.pe")
        assert (back.address, back.reference, back.notes) == (
            "Jr. Pizarro 450",
            "Frente al parque",
            "Sin cebolla",
        )
    finally:
        engine.dispose()


@pytest.fixture
def database_before_staff_encryption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    database = tmp_path / "antes-0047.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    command.upgrade(_config(), "0046")
    yield database
    get_settings.cache_clear()


# La hora de cada fila viaja como `:t`.
_BEFORE_0047 = (
    (
        "INSERT INTO restaurants (id, name, slug, is_active, timezone, created_at) "
        "VALUES (1, 'Local', 'local', 1, 'America/Lima', :t)"
    ),
    (
        "INSERT INTO roles (id, restaurant_id, name, name_key, kind, permissions, created_at) "
        "VALUES (1, 1, 'Encargado', 'encargado', 'owner', '[]', :t)"
    ),
    (
        "INSERT INTO users (id, restaurant_id, email, full_name, password_hash, is_active, "
        "created_at, role_id, deactivation_reason) VALUES "
        "(1, 1, 'rosa@local.pe', 'Rosa', 'x', 1, :t, 1, ''), "
        "(2, 1, 'luis@local.pe', 'Luis', 'x', 0, :t, 1, 'Renunció por salud')"
    ),
    (
        "INSERT INTO billing_settings (id, restaurant_id, ruc, legal_name, address, igv_rate, "
        "boleta_series, factura_series) VALUES "
        "(1, 1, '10456789123', 'Rosa Pérez', 'Jr. Pizarro 450', 18, 'B001', 'F001')"
    ),
    (
        "INSERT INTO menu_categories (id, restaurant_id, name, position, is_active, created_at) "
        "VALUES (1, 1, 'Fondos', 0, 1, :t)"
    ),
    (
        "INSERT INTO menu_items (id, restaurant_id, category_id, name, description, price, "
        "is_available, is_active, position, created_at) "
        "VALUES (1, 1, 1, 'Ají de gallina', '', 10, 1, 1, 0, :t)"
    ),
    (
        "INSERT INTO orders (id, restaurant_id, number, business_date, type, status, "
        "waiter_id, customer_name, notes, total, cancel_reason, created_at, updated_at, "
        "status_changed_at) "
        "VALUES (1, 1, 1, '2026-10-01', 'dine_in', 'paid', 1, '', '', 10, '', :t, :t, :t)"
    ),
    (
        "INSERT INTO order_items (id, restaurant_id, order_id, menu_item_id, name, "
        "unit_price, quantity, notes, created_at) "
        "VALUES (1, 1, 1, 1, 'Ají de gallina', 10, 1, 'Sin maní', :t)"
    ),
)
_MOMENT = {"t": "2026-10-01 20:00:00"}


def test_0047_encrypts_staff_notes_and_fiscal_data_and_can_be_undone(
    database_before_staff_encryption: Path,
) -> None:
    engine = create_engine(f"sqlite:///{database_before_staff_encryption.as_posix()}")
    cipher = get_cipher()
    try:
        with engine.begin() as conn:
            for statement in _BEFORE_0047:
                conn.execute(text(statement), _MOMENT)

        command.upgrade(_config(), "head")

        with engine.connect() as conn:
            owner, former = conn.execute(text("SELECT * FROM users ORDER BY id")).all()
            fiscal = conn.execute(
                text("SELECT ruc, address, legal_name FROM billing_settings")
            ).one()
            note = conn.execute(text("SELECT notes FROM order_items")).scalar_one()
        assert owner.email.startswith("enc1:")
        assert cipher.decrypt(owner.email) == "rosa@local.pe"
        assert owner.email_key == email_index("Rosa@Local.pe ")
        assert owner.deactivation_reason == ""
        assert cipher.decrypt(former.deactivation_reason) == "Renunció por salud"
        assert fiscal.ruc.startswith("enc1:")
        assert cipher.decrypt(fiscal.ruc) == "10456789123"
        assert cipher.decrypt(fiscal.address) == "Jr. Pizarro 450"
        # La razón social queda en claro: sale en cada comprobante.
        assert fiscal.legal_name == "Rosa Pérez"
        assert cipher.decrypt(note) == "Sin maní"
        # El correo sigue siendo único, ahora por su índice ciego.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (restaurant_id, email, email_key, full_name, "
                    "password_hash, is_active, created_at, role_id, deactivation_reason) "
                    "VALUES (1, 'otro', :key, 'Otra', 'x', 1, :t, 1, '')"
                ),
                {"key": owner.email_key, **_MOMENT},
            )

        command.downgrade(_config(), "0046")

        with engine.connect() as conn:
            emails = conn.execute(text("SELECT email FROM users ORDER BY id")).scalars().all()
            back = conn.execute(text("SELECT ruc, address FROM billing_settings")).one()
            note = conn.execute(text("SELECT notes FROM order_items")).scalar_one()
            columns = {c["name"] for c in inspect(conn).get_columns("users")}
        assert emails == ["rosa@local.pe", "luis@local.pe"]
        assert (back.ruc, back.address, note) == ("10456789123", "Jr. Pizarro 450", "Sin maní")
        assert "email_key" not in columns
    finally:
        engine.dispose()


def _rotated(customers: int, users: int) -> dict[str, int]:
    return {
        "billing_settings": 0,
        "credit_notes": 0,
        "customers": customers,
        "invoices": 0,
        "order_items": 0,
        "orders": 0,
        "reservations": 0,
        "two_factor_credentials": 0,
        "users": users,
    }


async def test_rotation_script_reencrypts_with_the_new_key_and_rebuilds_the_index(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_key, new_key = generate_key(), generate_key()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", old_key)
    get_settings.cache_clear()
    # El local se crea con la llave vieja: sus dos cuentas también rotan.
    local = await staffed_restaurant(session, "local-rotacion")
    session.add(
        CustomerRow(
            restaurant_id=local.id,
            name="Ana",
            phone=PHONE,
            phone_key=get_cipher().blind_index("987654321"),
            notes="Sin cebolla",
        )
    )
    await session.commit()
    old_key_id = (await _raw(session, "SELECT notes FROM customers"))[0].notes.split(":")[1]

    monkeypatch.setenv("DATA_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("DATA_ENCRYPTION_PREVIOUS_KEYS", old_key)
    get_settings.cache_clear()
    assert await rotate(session, dry_run=True) == _rotated(customers=1, users=2)
    assert await rotate(session) == _rotated(customers=1, users=2)
    await session.commit()

    stored = (await _raw(session, "SELECT phone, phone_key, notes FROM customers"))[0]
    assert stored.notes.split(":")[1] != old_key_id
    assert stored.phone_key == get_cipher().blind_index("987654321")
    customer = (await session.execute(select(CustomerRow))).scalar_one()
    assert (customer.phone, customer.notes) == (PHONE, "Sin cebolla")
    # El índice del correo se rehízo con la llave nueva: la cuenta se sigue encontrando.
    session.expire_all()
    found = await SqlAlchemyUserRepository(session).get_by_email("encargado@local-rotacion.pe")
    assert found is not None
    assert found.id == local.admin.id
    assert await rotate(session) == _rotated(customers=0, users=0)

    get_settings.cache_clear()


def test_every_encrypted_column_is_known_to_the_rotation_script() -> None:
    assert encrypted_columns() == {
        "billing_settings": ["ruc", "address", "sol_user", "sol_password", "certificate_pem"],
        "credit_notes": ["signed_xml", "cdr_zip", "document_payload"],
        "customers": ["phone", "email", "address", "reference", "notes"],
        "invoices": [
            "customer_document_number",
            "customer_address",
            "signed_xml",
            "cdr_zip",
            "document_payload",
        ],
        "order_items": ["notes"],
        "orders": ["customer_phone", "delivery_address", "delivery_reference", "notes"],
        "reservations": ["phone", "notes"],
        "two_factor_credentials": ["secret"],
        "users": ["email", "deactivation_reason"],
    }


async def test_rotating_the_key_keeps_two_factor_working(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_key, new_key = generate_key(), generate_key()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", old_key)
    get_settings.cache_clear()
    clock = FixedClock()
    store = SqlTwoFactorStore(clock=clock)
    secrets = {}
    # Varias cuentas: la llave primaria es compuesta y el script recorre por la tupla.
    for kind, account_id in (("platform", 1), ("staff", 1), ("staff", 2)):
        secret = await store.begin(session, kind, account_id)
        assert await store.confirm(
            session, kind, account_id, totp(secret, current_step(clock.now()))
        )
        secrets[kind, account_id] = secret
    await session.commit()
    before = (await _raw(session, "SELECT secret FROM two_factor_credentials"))[0].secret
    assert before.startswith("enc1:")
    assert all(value not in before for value in secrets.values())

    monkeypatch.setenv("DATA_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("DATA_ENCRYPTION_PREVIOUS_KEYS", old_key)
    get_settings.cache_clear()
    assert (await rotate(session))["two_factor_credentials"] == 3
    await session.commit()
    assert (await rotate(session))["two_factor_credentials"] == 0

    # Sin la llave vieja: solo se lee lo que quedó con la nueva.
    monkeypatch.setenv("DATA_ENCRYPTION_PREVIOUS_KEYS", "")
    get_settings.cache_clear()
    session.expunge_all()
    rows = await _raw(session, "SELECT secret FROM two_factor_credentials")
    assert all(row.secret.split(":")[1] != before.split(":")[1] for row in rows)
    clock.advance(seconds=30)
    for (kind, account_id), secret in secrets.items():
        code = totp(secret, current_step(clock.now()))
        assert await store.verify(session, kind, account_id, code) is SecondFactor.TOTP

    get_settings.cache_clear()
