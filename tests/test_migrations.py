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
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from resthub.core.config import get_settings
from resthub.core.database import Base
from resthub.core.permissions import Permission
from tests.conftest import REGISTERED_MODELS, url

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


def test_migrations_reproduce_the_model(migrated_database: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == [], (
        "El modelo y las migraciones divergieron. Generá la migración que falta con "
        "`uv run alembic revision --autogenerate -m '...'`."
    )


# Las de la administración del sistema no son de ningún local, a propósito.
PLATFORM_TABLES = {"platform_admins", "platform_activity"}
# Un código de vista previa apunta a una cuenta, que ya dice de qué local es.
PREVIEW_TABLES = {"preview_codes"}
# La telemetría del panel de observabilidad no es negocio de ningún local:
# guarda `restaurant_id` cuando lo hay, como entero suelto y opcional, y muchas
# filas (el acceso, las rutas de plataforma) no tienen restaurante.
TELEMETRY_TABLES = {"obs_requests", "obs_events"}
# Los intentos de acceso se cuentan antes de saber de qué cuenta (y de qué local)
# es el correo, y el historial también guarda los de la plataforma: el
# restaurante, cuando lo hay, va como entero suelto y opcional.
ACCESS_TABLES = {"login_attempts", "access_history"}


def test_every_business_table_carries_restaurant(migrated_database: Path) -> None:
    """La frontera entre restaurantes empieza en el esquema, no en el código."""
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        inspector = inspect(engine)
        tables = (
            set(inspector.get_table_names())
            - {"alembic_version", "restaurants"}
            - PLATFORM_TABLES
            - PREVIEW_TABLES
            - TELEMETRY_TABLES
            - ACCESS_TABLES
        )
        without_restaurant = {
            table
            for table in tables
            if "restaurant_id" not in {column["name"] for column in inspector.get_columns(table)}
        }
    finally:
        engine.dispose()

    assert tables
    assert without_restaurant == set()


def test_migration_can_be_undone(migrated_database: Path) -> None:
    command.downgrade(_config(), "base")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()


def _order_with_payments(database: Path, status: str, payments: list[tuple[str, str]]) -> None:
    """Un pedido con sus pagos, cargado a mano en un esquema de la 0007 en adelante."""
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    now = "2026-10-01 20:00:00"
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO restaurants (id, name, slug, is_active, timezone, created_at) "
                    "VALUES (1, 'Local', 'local', 1, 'America/Lima', :t)"
                ),
                {"t": now},
            )
            conn.execute(
                text(
                    "INSERT INTO orders (id, restaurant_id, number, business_date, type, status, "
                    "waiter_id, customer_name, notes, total, cancel_reason, payment_method, "
                    "created_at, updated_at, status_changed_at) VALUES (1, 1, 1, '2026-10-01', "
                    "'takeaway', :s, 1, '', '', '50.00', '', :m, :t, :t, :t)"
                ),
                {"s": status, "m": "mixed" if len(payments) > 1 else None, "t": now},
            )
            for method_name, amount in payments:
                conn.execute(
                    text(
                        "INSERT INTO order_payments (restaurant_id, order_id, method, amount, "
                        "tip, received_by, created_at) VALUES (1, 1, :m, :a, 0, 1, :t)"
                    ),
                    {"m": method_name, "a": amount, "t": now},
                )
    finally:
        engine.dispose()


def test_undoing_cash_register_keeps_the_largest_payment_method(migrated_database: Path) -> None:
    command.downgrade(_config(), "0007")
    _order_with_payments(migrated_database, "paid", [("cash", "20.00"), ("yape", "30.00")])

    command.downgrade(_config(), "0006")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as conn:
            method = conn.execute(text("SELECT payment_method FROM orders")).scalar_one()
    finally:
        engine.dispose()
    assert method == "yape"


def test_cash_register_is_not_undone_with_open_partial_payments(migrated_database: Path) -> None:
    command.downgrade(_config(), "0007")
    _order_with_payments(migrated_database, "served", [("cash", "20.00")])

    with pytest.raises(RuntimeError, match="pagos parciales"):
        command.downgrade(_config(), "0006")


def test_undoing_delivery_leaves_it_as_takeaway(migrated_database: Path) -> None:
    command.downgrade(_config(), "0012")
    _order_with_payments(migrated_database, "paid", [("cash", "50.00")])
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
            order_type = conn.execute(text("SELECT type FROM orders")).scalar_one()
    finally:
        engine.dispose()
    assert order_type == "takeaway"


def _restaurant_with_staff(database: Path) -> None:
    """Un restaurante con un encargado y un mesero, en el esquema de la 0012."""
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    now = "2026-10-01 20:00:00"
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO restaurants (id, name, slug, is_active, timezone, created_at) "
                    "VALUES (1, 'Local', 'local', 1, 'America/Lima', :t)"
                ),
                {"t": now},
            )
            for user_id, role in ((1, "admin"), (2, "waiter")):
                conn.execute(
                    text(
                        "INSERT INTO users (id, restaurant_id, email, full_name, role, "
                        "password_hash, is_active, created_at) VALUES (:id, 1, :email, 'X', "
                        ":role, 'hash', 1, :t)"
                    ),
                    {"id": user_id, "email": f"{role}@local.pe", "role": role, "t": now},
                )
    finally:
        engine.dispose()


def test_fixed_roles_become_the_base_roles_of_each_restaurant(migrated_database: Path) -> None:
    command.downgrade(_config(), "0012")
    _restaurant_with_staff(migrated_database)

    command.upgrade(_config(), "0013")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as conn:
            roles = conn.execute(
                text("SELECT id, name, kind, permissions FROM roles WHERE restaurant_id = 1")
            ).all()
            accounts = dict(
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
    waiter = next(json.loads(permissions) for _, _, kind, permissions in roles if kind == "waiter")
    assert Permission.ORDERS_CHARGE.value in waiter
    assert Permission.STAFF_MANAGE.value not in waiter
    assert accounts == {1: "owner", 2: "waiter"}


def test_undoing_roles_leaves_admin_for_the_owner_and_waiter_for_the_rest(
    migrated_database: Path,
) -> None:
    command.downgrade(_config(), "0012")
    _restaurant_with_staff(migrated_database)
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
            tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert roles == {1: "admin", 2: "waiter"}
    assert "roles" not in tables


def test_platform_accounts_have_no_restaurant_or_role(migrated_database: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("platform_admins")}
        foreign_keys = inspector.get_foreign_keys("platform_activity")
        indexes = {index["name"]: index for index in inspector.get_indexes("platform_admins")}
    finally:
        engine.dispose()
    assert columns == {
        "id",
        "email",
        "full_name",
        "password_hash",
        "is_active",
        "created_at",
        "sessions_valid_since",
    }
    assert [
        (foreign_key["referred_table"], foreign_key["constrained_columns"])
        for foreign_key in foreign_keys
    ] == [("platform_admins", ["admin_id"])]
    assert indexes["ix_platform_admins_email"]["unique"]


def test_undoing_platform_drops_only_its_tables(migrated_database: Path) -> None:
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
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert not tables & PLATFORM_TABLES
    assert {"restaurants", "users", "roles"} <= tables

    command.upgrade(_config(), "head")


def test_pre_existing_restaurants_are_not_sandbox(migrated_database: Path) -> None:
    command.downgrade(_config(), "0014")
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO restaurants (id, name, slug, is_active, timezone, "
                    "max_waiter_discount_percent, auto_out_of_stock, created_at) "
                    "VALUES (1, 'Local', 'local', 1, 'America/Lima', 10, 1, :t)"
                ),
                {"t": "2026-10-01 20:00:00"},
            )
    finally:
        engine.dispose()

    command.upgrade(_config(), "head")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as conn:
            sandbox_flag = conn.execute(
                text("SELECT is_sandbox FROM restaurants WHERE id = 1")
            ).scalar()
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("preview_codes")}
        foreign_keys = {
            (foreign_key["referred_table"], tuple(foreign_key["constrained_columns"]))
            for foreign_key in inspector.get_foreign_keys("preview_codes")
        }
        indexes = {index["name"]: index for index in inspector.get_indexes("preview_codes")}
    finally:
        engine.dispose()
    assert sandbox_flag == 0
    assert columns == {
        "id",
        "code_hash",
        "user_id",
        "platform_admin_id",
        "expires_at",
        "used_at",
        "created_at",
    }
    assert foreign_keys == {("users", ("user_id",)), ("platform_admins", ("platform_admin_id",))}
    assert indexes["ix_preview_codes_code_hash"]["unique"]


def _restaurant(
    conn: Connection, restaurant_id: int, slug: str, sandbox: bool, active: bool
) -> None:
    conn.execute(
        text(
            "INSERT INTO restaurants (id, name, slug, is_active, timezone, "
            "max_waiter_discount_percent, auto_out_of_stock, is_sandbox, created_at) "
            "VALUES (:id, 'Local', :slug, :active, 'America/Lima', 10, 1, :sandbox, :t)"
        ),
        {
            "id": restaurant_id,
            "slug": slug,
            "active": active,
            "sandbox": sandbox,
            "t": "2026-10-01 20:00:00",
        },
    )


def test_migration_leaves_a_single_active_sandbox_restaurant(migrated_database: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.begin() as conn:
            _restaurant(conn, 1, "muestra-00000001", sandbox=True, active=True)
            # Archivados y reales, los que sean.
            _restaurant(conn, 2, "archivado-2-00000000", sandbox=True, active=False)
            _restaurant(conn, 3, "archivado-3-00000000", sandbox=True, active=False)
            _restaurant(conn, 4, "real", sandbox=False, active=True)
            _restaurant(conn, 5, "otro-real", sandbox=False, active=True)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _restaurant(conn, 6, "muestra-00000006", sandbox=True, active=True)
    finally:
        engine.dispose()


def test_undoing_preview_drops_the_codes_and_the_flag(migrated_database: Path) -> None:
    command.downgrade(_config(), "0014")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("restaurants")}
    finally:
        engine.dispose()
    assert not tables & PREVIEW_TABLES
    assert tables >= PLATFORM_TABLES
    assert "is_sandbox" not in columns

    command.upgrade(_config(), "head")


def test_undoing_preview_deactivates_the_sandbox_restaurants(
    migrated_database: Path,
) -> None:
    """Sin la marca serían restaurantes reales y activos: quedan desactivados."""
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.begin() as conn:
            _restaurant(conn, 1, "muestra-00000001", sandbox=True, active=True)
            _restaurant(conn, 2, "archivado-2-00000000", sandbox=True, active=False)
            _restaurant(conn, 3, "real", sandbox=False, active=True)
    finally:
        engine.dispose()

    command.downgrade(_config(), "0014")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.connect() as conn:
            states = dict(
                conn.execute(text("SELECT id, is_active FROM restaurants ORDER BY id")).all()
            )
    finally:
        engine.dispose()
    assert states == {1: 0, 2: 0, 3: 1}

    command.upgrade(_config(), "head")


def test_telemetry_has_no_foreign_keys_and_is_indexed_by_time(
    migrated_database: Path,
) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        inspector = inspect(engine)
        foreign_keys = {table: inspector.get_foreign_keys(table) for table in TELEMETRY_TABLES}
        indexes = {
            table: {index["name"]: index["column_names"] for index in inspector.get_indexes(table)}
            for table in TELEMETRY_TABLES
        }
        columns = {column["name"] for column in inspector.get_columns("obs_requests")}
    finally:
        engine.dispose()
    assert foreign_keys == {"obs_requests": [], "obs_events": []}
    assert indexes["obs_requests"] == {
        "ix_obs_requests_at": ["at"],
        "ix_obs_requests_route_at": ["route", "at"],
        "ix_obs_requests_request_id": ["request_id"],
    }
    assert indexes["obs_events"] == {
        "ix_obs_events_at": ["at"],
        "ix_obs_events_level_at": ["level", "at"],
        "ix_obs_events_request_id": ["request_id"],
    }
    # Nada de la petición más allá de su plantilla: ni query string ni cabeceras.
    assert columns == {
        "id",
        "at",
        "method",
        "route",
        "status",
        "duration_ms",
        "db_ms",
        "db_queries",
        "request_id",
        "account_kind",
        "restaurant_id",
        "account_id",
    }


def test_undoing_telemetry_drops_only_its_tables(migrated_database: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO obs_requests (at, method, route, status, duration_ms, db_ms, "
                    "db_queries, request_id, account_kind, restaurant_id, account_id) VALUES "
                    "(:t, 'GET', :route, 200, 12.5, 3.1, 2, 'abc12345', 'staff', 1, 1)"
                ),
                {"t": "2026-10-01 20:00:00", "route": url("orders")},
            )
    finally:
        engine.dispose()

    command.downgrade(_config(), "0015")

    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert not tables & TELEMETRY_TABLES
    assert {"restaurants", "preview_codes", "platform_admins"} <= tables

    command.upgrade(_config(), "head")


@pytest.mark.parametrize(
    ("table", "columns", "values"),
    [
        (
            "activity_log",
            "restaurant_id, user_id, kind, detail, occurred_at",
            "1, 1, 'asiento', '', '2026-10-01 20:00:00'",
        ),
        ("platform_activity", "admin_id, kind, detail, created_at", "1, 'asiento', '', :t"),
    ],
)
def test_logs_are_append_only(
    migrated_database: Path, table: str, columns: str, values: str
) -> None:
    engine = create_engine(f"sqlite:///{migrated_database.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO {table} ({columns}) VALUES ({values})"),  # noqa: S608
                {"t": "2026-10-01 20:00:00"},
            )
        for statement in (f"UPDATE {table} SET kind = 'otro'", f"DELETE FROM {table}"):  # noqa: S608
            with pytest.raises(DBAPIError, match="solo inserción"), engine.begin() as conn:
                conn.execute(text(statement))
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT kind FROM {table}")).scalar_one() == "asiento"  # noqa: S608
    finally:
        engine.dispose()
