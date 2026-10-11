"""Infraestructura compartida de las pruebas.

Por omisión, cada prueba recibe una base SQLite en memoria propia, así que el orden
en que corren no puede influir en el resultado. `StaticPool` es imprescindible: sin
él cada conexión abriría su propia base en memoria y las tablas creadas por una no
existirían para la siguiente.

Con `RESTHUB_TEST_DATABASE_URL=postgresql+asyncpg://...` la misma suite corre contra
PostgreSQL, como en producción (job `backend-postgres` del CI). Cada proceso de
pytest (cada worker de xdist) crea su propio esquema con las tablas; antes de cada
prueba se vacían, así que tampoco ahí el orden puede influir.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from hypothesis import settings
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRow
from resthub.core.auth import get_token_service
from resthub.core.background import BackgroundJobs, get_background_jobs
from resthub.core.clock import SystemClock
from resthub.core.clock_dependency import get_clock
from resthub.core.config import Settings
from resthub.core.database import Base, get_session, get_session_factory
from resthub.core.llm import JsonCompletion, JsonCompletionRequest, LlmUnavailableError
from resthub.core.llm_openrouter import get_llm_client
from resthub.core.login_throttle import (
    LoginThrottle,
    get_login_throttle,
    get_platform_login_throttle,
)
from resthub.core.permissions import Permission
from resthub.core.realtime_broker import LocalBroker, get_broker
from resthub.core.security import hasher_from_settings
from resthub.core.tokens import JwtTokenService
from resthub.main import API_PREFIX, create_app
from resthub.modules.accounts.adapters.api.dependencies import get_password_hasher
from resthub.modules.accounts.adapters.persistence import models as accounts_models
from resthub.modules.accounts.adapters.persistence.sqlalchemy_role_repository import (
    SqlAlchemyRoleRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.roles import Role, RoleKind
from resthub.modules.accounts.use_cases.manage_roles import ensure_base_roles
from resthub.modules.billing.adapters.persistence import models as billing_models
from resthub.modules.customers.adapters.persistence import models as customers_models
from resthub.modules.insights.adapters.ai.rule_based_engine import RuleBasedDecisionEngine
from resthub.modules.insights.adapters.ai.selector import DecisionEngineSelector
from resthub.modules.insights.adapters.api.dependencies import get_decision_engine
from resthub.modules.insights.adapters.persistence import models as insights_models
from resthub.modules.insights.ports.decision_engine import DecisionEngineGateway
from resthub.modules.inventory.adapters.persistence import models as inventory_models
from resthub.modules.menu.adapters.persistence import models as menu_models
from resthub.modules.orders.adapters.persistence import models as orders_models
from resthub.modules.platform.adapters.api.dependencies import (
    get_password_hasher as get_platform_password_hasher,
)
from resthub.modules.platform.adapters.persistence import models as platform_models
from resthub.modules.reservations.adapters.persistence import models as reservations_models
from resthub.modules.restaurants.adapters.persistence import models as restaurants_models
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain.entities import Restaurant

__all__ = ["API_PREFIX", "url"]

# Perfiles de Hypothesis. El normal es corto para que la suite siga siendo rápida; el profundo
# busca más ejemplos: `pytest --hypothesis-profile=deep tests/test_properties_*.py`.
settings.register_profile("default", deadline=None)
settings.register_profile("deep", deadline=None, max_examples=5000)
settings.load_profile("default")


def url(path: str = "") -> str:
    """Ruta absoluta de la API: `url("orders")` da `/api/v1/orders`."""
    return f"{API_PREFIX}/{path}" if path else API_PREFIX


# Cuatro rondas en vez de doce. El algoritmo es el mismo que en producción, que
# es lo que interesa probar; el costo deliberado no aporta nada a una prueba.
# El costo mínimo de Argon2id, pedido por configuración como lo haría un despliegue: el hash de
# producción tarda decenas de milisegundos y la suite crea cientos de cuentas.
TEST_HASHER = hasher_from_settings(
    Settings(
        password_argon2_time_cost=1,
        password_argon2_memory_kib=8,
        password_argon2_parallelism=1,
    )
)
TEST_TOKEN_SERVICE = JwtTokenService(
    secret_key="secreto-de-prueba-con-largo-suficiente",
    algorithm="HS256",
    ttl_seconds=3600,
)
# Los modelos se importan para que sus tablas queden registradas en
# `Base.metadata` antes de crearlas. La tupla hace explícita esa intención.
REGISTERED_MODELS = (
    ActivityRow,
    accounts_models,
    billing_models,
    customers_models,
    insights_models,
    inventory_models,
    menu_models,
    orders_models,
    platform_models,
    reservations_models,
    restaurants_models,
)

VALID_PASSWORD = "contrasena-larga"


class DatabaseUnderTest(BaseSettings):
    """Base contra la que corre la suite; se elige con `RESTHUB_TEST_DATABASE_URL`.

    Se lee con pydantic-settings, como el resto de la configuración, y no con
    `os.environ` (ruff `TID251`).
    """

    model_config = SettingsConfigDict(env_prefix="RESTHUB_TEST_", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///:memory:"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


DATABASE_UNDER_TEST = DatabaseUnderTest()


@pytest.fixture(scope="session")
def postgres_schema() -> Iterator[str | None]:
    """El esquema de este proceso en PostgreSQL, con todas las tablas; `None` en SQLite.

    Es síncrona a propósito: crear y borrar el esquema es una vez por proceso y no
    necesita el bucle de eventos de ninguna prueba.
    """
    if not DATABASE_UNDER_TEST.is_postgres:
        yield None
        return
    schema = f"test_{uuid.uuid4().hex}"

    async def run(statement: str | None) -> None:
        engine = _postgres_engine(schema if statement is None else None)
        async with engine.begin() as connection:
            if statement is None:
                await connection.run_sync(Base.metadata.create_all)
            else:
                await connection.execute(text(statement))
        await engine.dispose()

    asyncio.run(run(f'CREATE SCHEMA "{schema}"'))
    asyncio.run(run(None))
    yield schema
    asyncio.run(run(f'DROP SCHEMA "{schema}" CASCADE'))


def _postgres_engine(schema: str | None) -> AsyncEngine:
    connect_args: dict[str, object] = {"statement_cache_size": 0}
    if schema is not None:
        connect_args["server_settings"] = {"search_path": schema}
    return create_async_engine(
        DATABASE_UNDER_TEST.database_url, poolclass=NullPool, connect_args=connect_args
    )


@pytest.fixture
async def session(postgres_schema: str | None) -> AsyncIterator[AsyncSession]:
    if postgres_schema is None:
        engine = create_async_engine(
            DATABASE_UNDER_TEST.database_url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    else:
        engine = _postgres_engine(postgres_schema)
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as open_session:
        yield open_session

    await engine.dispose()


@pytest.fixture
def users(session: AsyncSession) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(session)


class FakeLlmClient:
    """Modelo de prueba: anota lo que se le pide y responde lo configurado.

    Con `response = None` se comporta como una IA apagada.
    """

    def __init__(self) -> None:
        self.requests: list[JsonCompletionRequest] = []
        self.response: dict[str, object] | None = {"respuesta": "ok"}

    async def complete_json(self, request: JsonCompletionRequest) -> JsonCompletion:
        self.requests.append(request)
        if self.response is None:
            message = "La IA no está configurada en este servidor."
            raise LlmUnavailableError(message)
        return JsonCompletion(data=self.response, model="modelo-de-prueba")


@pytest.fixture
def llm() -> FakeLlmClient:
    """Ninguna prueba llama a un modelo real, aunque el `.env` tenga una clave."""
    return FakeLlmClient()


@pytest.fixture
def broker() -> LocalBroker:
    """Reparto de avisos en memoria: ninguna prueba abre `LISTEN` en Postgres."""
    return LocalBroker()


@pytest.fixture
def jobs() -> BackgroundJobs:
    """Tareas en segundo plano propias de cada prueba, para poder esperarlas."""
    return BackgroundJobs()


@pytest.fixture
def decision_engine() -> DecisionEngineGateway:
    """Las reglas fijas, como un servidor sin clave de TypeSafe.

    Ninguna prueba llama a Jev de verdad, aunque el `.env` tenga una clave. Las
    que prueban Jev lo hacen con un transporte HTTP falso.
    """
    return DecisionEngineSelector(rules=RuleBasedDecisionEngine(), jev=None, min_confidence=0.5)


class FakeClock:
    """La hora real más un adelanto que la prueba controla, para ver vencer un código.

    Sigue corriendo (los tokens de prueba se firman con la hora real y las cuentas
    cortan sesiones por instante). Una prueba que necesita «hoy» fijo reemplaza
    `get_clock` por un `FixedClock` (tests/fakes.py).
    """

    def __init__(self) -> None:
        self.offset = timedelta()
        self._system = SystemClock()

    def now(self) -> datetime:
        return self._system.now() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(
    session: AsyncSession,
    broker: LocalBroker,
    llm: FakeLlmClient,
    jobs: BackgroundJobs,
    decision_engine: DecisionEngineGateway,
    clock: FakeClock,
) -> FastAPI:
    """La aplicación con sus dependencias externas reemplazadas por las de prueba."""
    app = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        # Se replica el ciclo de `get_session`, confirmar al terminar y
        # deshacer ante un error, para que las pruebas ejerciten la misma
        # transacción que sirve una petición real.
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_session_factory] = lambda: async_sessionmaker(
        session.bind, expire_on_commit=False, class_=AsyncSession
    )
    app.dependency_overrides[get_password_hasher] = lambda: TEST_HASHER
    app.dependency_overrides[get_platform_password_hasher] = lambda: TEST_HASHER
    app.dependency_overrides[get_token_service] = lambda: TEST_TOKEN_SERVICE
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_background_jobs] = lambda: jobs
    app.dependency_overrides[get_decision_engine] = lambda: decision_engine
    app.dependency_overrides[get_clock] = lambda: clock
    # Cada prueba arranca sin intentos fallidos acumulados por otra.
    throttle = LoginThrottle()
    app.dependency_overrides[get_login_throttle] = lambda: throttle
    platform_throttle = LoginThrottle()
    app.dependency_overrides[get_platform_login_throttle] = lambda: platform_throttle
    return app


@pytest.fixture
async def client(app: FastAPI, jobs: BackgroundJobs) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    # Nada queda corriendo contra una base que la prueba ya cerró.
    await jobs.drain()


def build_user(
    restaurant_id: int,
    email: str,
    role: Role,
    full_name: str = "Ana Quispe",
    is_active: bool = True,
) -> User:
    return User(
        restaurant_id=restaurant_id,
        email=email,
        full_name=full_name,
        role=role,
        password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        is_active=is_active,
    )


def authorization_for(user: User) -> dict[str, str]:
    """Cabecera de acceso para un usuario ya persistido."""
    if user.id is None:
        message = "El usuario debe estar persistido para emitirle un token."
        raise ValueError(message)
    token = TEST_TOKEN_SERVICE.issue(user.id, user.restaurant_id)
    return {"Authorization": f"Bearer {token.value}"}


@dataclass(frozen=True, slots=True)
class StaffedRestaurant:
    """Un restaurante con sus dos roles base, un encargado y un mesero, ya guardados."""

    restaurant: Restaurant
    admin: User
    waiter: User

    @property
    def id(self) -> int:
        return self.restaurant.id or 0


async def staffed_restaurant(
    session: AsyncSession, slug: str, is_sandbox: bool = False
) -> StaffedRestaurant:
    restaurant = await SqlAlchemyRestaurantRepository(session).add(
        Restaurant(name=f"Restaurante {slug}", slug=slug, is_sandbox=is_sandbox)
    )
    roles = await ensure_base_roles(SqlAlchemyRoleRepository(session), restaurant.id or 0)
    users = SqlAlchemyUserRepository(session)
    admin = await users.add(
        build_user(restaurant.id or 0, f"encargado@{slug}.pe", roles.owner, full_name="Rosa Pérez")
    )
    waiter = await users.add(
        build_user(restaurant.id or 0, f"mesero@{slug}.pe", roles.waiter, full_name="Luis Torres")
    )
    await session.commit()
    return StaffedRestaurant(restaurant=restaurant, admin=admin, waiter=waiter)


async def staff_with_role(
    session: AsyncSession, local: StaffedRestaurant, name: str, permissions: set[str]
) -> User:
    """Una cuenta más del local, con un rol propio que solo tiene esos permisos."""
    slug = local.restaurant.slug
    role = await SqlAlchemyRoleRepository(session).add(
        Role(
            local.id,
            name,
            RoleKind.CUSTOM,
            stored_permissions=frozenset(Permission(code) for code in permissions),
        )
    )
    email = f"{name.lower().replace(' ', '-')}@{slug}.pe"
    user = await SqlAlchemyUserRepository(session).add(build_user(local.id, email, role))
    await session.commit()
    return user


@pytest.fixture
async def restaurant_a(session: AsyncSession) -> StaffedRestaurant:
    return await staffed_restaurant(session, "local-a")


@pytest.fixture
async def restaurant_b(session: AsyncSession) -> StaffedRestaurant:
    return await staffed_restaurant(session, "local-b")


class RecordingActivity:
    """Bitácora en memoria, para afirmar qué asientos dejó un caso de uso."""

    def __init__(self) -> None:
        self.entries: list[tuple[int, int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((restaurant_id, user_id, kind, detail))

    def kinds(self) -> list[ActivityKind]:
        return [kind for _, _, kind, _ in self.entries]
