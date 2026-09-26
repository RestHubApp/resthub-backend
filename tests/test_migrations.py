"""Las migraciones tienen que reproducir el modelo, no parecerse a él.

Sin esta prueba la deriva es silenciosa: alguien agrega una columna al modelo,
las pruebas pasan porque crean las tablas desde el metadata, y el despliegue
falla contra una base migrada que no tiene esa columna.

Es una prueba síncrona a propósito: `alembic` abre su propio bucle de eventos y
no puede anidarse dentro del que abriría pytest-asyncio.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from resthub.core.config import get_settings
from resthub.core.database import Base
from tests.conftest import REGISTERED_MODELS

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# El metadata tiene que estar completo antes de comparar: sin los modelos
# registrados, la comparación no vería ninguna tabla y pasaría siempre.
assert REGISTERED_MODELS


def _config() -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    database = tmp_path / "migrada.db"

    # `env.py` toma la URL de la configuración de la aplicación, así que hay que
    # apuntarla al archivo temporal y limpiar la caché que la memoriza.
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    get_settings.cache_clear()

    command.upgrade(_config(), "head")

    yield database

    get_settings.cache_clear()


def test_las_migraciones_reproducen_el_modelo(migrated_database: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diferencias = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diferencias == [], (
        "El modelo y las migraciones divergieron. Generá la migración que falta con "
        "`uv run alembic revision --autogenerate -m '...'`."
    )


# Las de la administración del sistema no son de ningún local, a propósito.
PLATFORM_TABLES = {"platform_admins", "platform_activity"}


def test_toda_tabla_de_negocio_lleva_restaurante(migrated_database: Path) -> None:
    """La frontera entre restaurantes empieza en el esquema, no en el código."""
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        inspector = inspect(engine)
        tablas = (
            set(inspector.get_table_names()) - {"alembic_version", "restaurants"} - PLATFORM_TABLES
        )
        sin_restaurante = {
            tabla
            for tabla in tablas
            if "restaurant_id" not in {column["name"] for column in inspector.get_columns(tabla)}
        }
    finally:
        engine.dispose()

    assert tablas
    assert sin_restaurante == set()


def test_la_migracion_se_puede_deshacer(migrated_database: Path) -> None:
    command.downgrade(_config(), "base")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()


def _pedido_con_pagos(database: Path, status: str, pagos: list[tuple[str, str]]) -> None:
    """Un pedido con sus pagos, cargado a mano en un esquema de la 0007 en adelante."""
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    ahora = "2026-10-01 20:00:00"
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO restaurants (id, name, slug, is_active, timezone, created_at) "
                    "VALUES (1, 'Local', 'local', 1, 'America/Lima', :t)"
                ),
                {"t": ahora},
            )
            conn.execute(
                text(
                    "INSERT INTO orders (id, restaurant_id, number, business_date, type, status, "
                    "waiter_id, customer_name, notes, total, cancel_reason, payment_method, "
                    "created_at, updated_at, status_changed_at) VALUES (1, 1, 1, '2026-10-01', "
                    "'takeaway', :s, 1, '', '', '50.00', '', :m, :t, :t, :t)"
                ),
                {"s": status, "m": "mixed" if len(pagos) > 1 else None, "t": ahora},
            )
            for metodo, monto in pagos:
                conn.execute(
                    text(
                        "INSERT INTO order_payments (restaurant_id, order_id, method, amount, "
                        "tip, received_by, created_at) VALUES (1, 1, :m, :a, 0, 1, :t)"
                    ),
                    {"m": metodo, "a": monto, "t": ahora},
                )
    finally:
        engine.dispose()


def test_deshacer_la_caja_deja_el_medio_del_pago_mayor(migrated_database: Path) -> None:
    command.downgrade(_config(), "0007")
    _pedido_con_pagos(migrated_database, "paid", [("cash", "20.00"), ("yape", "30.00")])

    command.downgrade(_config(), "0006")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as conn:
            medio = conn.execute(text("SELECT payment_method FROM orders")).scalar_one()
    finally:
        engine.dispose()
    assert medio == "yape"


def test_no_se_deshace_la_caja_con_pagos_parciales_abiertos(migrated_database: Path) -> None:
    command.downgrade(_config(), "0007")
    _pedido_con_pagos(migrated_database, "served", [("cash", "20.00")])

    with pytest.raises(RuntimeError, match="pagos parciales"):
        command.downgrade(_config(), "0006")


def test_deshacer_el_delivery_lo_deja_como_para_llevar(migrated_database: Path) -> None:
    command.downgrade(_config(), "0012")
    _pedido_con_pagos(migrated_database, "paid", [("cash", "50.00")])
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE orders SET type = 'delivery'"))
    finally:
        engine.dispose()

    command.downgrade(_config(), "0011")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as conn:
            tipo = conn.execute(text("SELECT type FROM orders")).scalar_one()
    finally:
        engine.dispose()
    assert tipo == "takeaway"


def _local_con_personal(database: Path) -> None:
    """Un restaurante con un encargado y un mesero, en el esquema de la 0012."""
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    ahora = "2026-10-01 20:00:00"
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO restaurants (id, name, slug, is_active, timezone, created_at) "
                    "VALUES (1, 'Local', 'local', 1, 'America/Lima', :t)"
                ),
                {"t": ahora},
            )
            for user_id, role in ((1, "admin"), (2, "waiter")):
                conn.execute(
                    text(
                        "INSERT INTO users (id, restaurant_id, email, full_name, role, "
                        "password_hash, is_active, created_at) VALUES (:id, 1, :email, 'X', "
                        ":role, 'hash', 1, :t)"
                    ),
                    {"id": user_id, "email": f"{role}@local.pe", "role": role, "t": ahora},
                )
    finally:
        engine.dispose()


def test_los_roles_fijos_pasan_a_los_roles_base_de_cada_local(migrated_database: Path) -> None:
    command.downgrade(_config(), "0012")
    _local_con_personal(migrated_database)

    command.upgrade(_config(), "0013")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as conn:
            roles = conn.execute(
                text("SELECT id, name, kind, permissions FROM roles WHERE restaurant_id = 1")
            ).all()
            cuentas = dict(
                conn.execute(
                    text(
                        "SELECT u.id, r.kind FROM users u JOIN roles r ON r.id = u.role_id "
                        "ORDER BY u.id"
                    )
                ).all()
            )
    finally:
        engine.dispose()
    assert {(name, kind) for _, name, kind, _ in roles} == {
        ("Encargado", "owner"),
        ("Mesero", "waiter"),
    }
    mesero = next(json.loads(permisos) for _, _, kind, permisos in roles if kind == "waiter")
    assert "orders.charge" in mesero
    assert "staff.manage" not in mesero
    assert cuentas == {1: "owner", 2: "waiter"}


def test_deshacer_los_roles_deja_admin_al_encargado_y_waiter_al_resto(
    migrated_database: Path,
) -> None:
    command.downgrade(_config(), "0012")
    _local_con_personal(migrated_database)
    command.upgrade(_config(), "0013")
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO roles (id, restaurant_id, name, name_key, kind, permissions, "
                    "created_at) VALUES (99, 1, 'Cocinero', 'cocinero', 'custom', "
                    "'[\"orders.manage\"]', '2026-10-01 20:00:00')"
                )
            )
            conn.execute(text("UPDATE users SET role_id = 99 WHERE id = 2"))
    finally:
        engine.dispose()

    command.downgrade(_config(), "0012")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as conn:
            roles = dict(conn.execute(text("SELECT id, role FROM users ORDER BY id")).all())
            tablas = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert roles == {1: "admin", 2: "waiter"}
    assert "roles" not in tablas


def test_las_cuentas_de_plataforma_no_tienen_restaurante_ni_rol(migrated_database: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        inspector = inspect(engine)
        columnas = {column["name"] for column in inspector.get_columns("platform_admins")}
        claves = inspector.get_foreign_keys("platform_activity")
        indices = {index["name"]: index for index in inspector.get_indexes("platform_admins")}
    finally:
        engine.dispose()
    assert columnas == {"id", "email", "full_name", "password_hash", "is_active", "created_at"}
    assert [(clave["referred_table"], clave["constrained_columns"]) for clave in claves] == [
        ("platform_admins", ["admin_id"])
    ]
    assert indices["ix_platform_admins_email"]["unique"]


def test_deshacer_la_plataforma_borra_solo_sus_tablas(migrated_database: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO platform_admins (id, email, full_name, password_hash, is_active, "
                    "created_at) VALUES (1, 'equipo@resthub.dev', 'Equipo', 'hash', 1, :t)"
                ),
                {"t": "2026-10-01 20:00:00"},
            )
            conn.execute(
                text(
                    "INSERT INTO platform_activity (admin_id, kind, detail, created_at) "
                    "VALUES (1, 'signed_in', '', :t)"
                ),
                {"t": "2026-10-01 20:00:00"},
            )
    finally:
        engine.dispose()

    command.downgrade(_config(), "0013")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        tablas = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert not tablas & PLATFORM_TABLES
    assert {"restaurants", "users", "roles"} <= tablas

    command.upgrade(_config(), "head")
