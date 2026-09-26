"""Infraestructura compartida de las pruebas.

Cada prueba recibe una base SQLite en memoria propia, así que el orden en que
corren no puede influir en el resultado. `StaticPool` es imprescindible: sin él
cada conexión abriría su propia base en memoria y las tablas creadas por una no
existirían para la siguiente.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRow
from resthub.core.auth import get_token_service
from resthub.core.background import BackgroundJobs, get_background_jobs
from resthub.core.database import Base, get_session, get_session_factory
from resthub.core.llm import JsonCompletion, JsonCompletionRequest, LlmUnavailable
from resthub.core.llm_openrouter import get_llm_client
from resthub.core.login_throttle import LoginThrottle, get_login_throttle
from resthub.core.realtime_broker import LocalBroker, get_broker
from resthub.core.security import BcryptPasswordHasher
from resthub.core.tokens import JwtTokenService
from resthub.main import create_app
from resthub.modules.accounts.adapters.api.dependencies import get_password_hasher
from resthub.modules.accounts.adapters.persistence import models as accounts_models
from resthub.modules.accounts.adapters.persistence.sqlalchemy_role_repository import (
    SqlAlchemyRoleRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.accounts.use_cases.manage_roles import ensure_base_roles
from resthub.modules.billing.adapters.persistence import models as billing_models
from resthub.modules.customers.adapters.persistence import models as customers_models
from resthub.modules.insights.adapters.ai.rule_based_engine import RuleBasedDecisionEngine
from resthub.modules.insights.adapters.ai.selector import DecisionEngineSelector
from resthub.modules.insights.adapters.api.dependencies import get_decision_engine
from resthub.modules.insights.adapters.persistence import models as insights_models
from resthub.modules.insights.ports.decision_engine import DecisionEngine
from resthub.modules.inventory.adapters.persistence import models as inventory_models
from resthub.modules.menu.adapters.persistence import models as menu_models
from resthub.modules.orders.adapters.persistence import models as orders_models
from resthub.modules.reservations.adapters.persistence import models as reservations_models
from resthub.modules.restaurants.adapters.persistence import models as restaurants_models
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain.entities import Restaurant

# Cuatro rondas en vez de doce. El algoritmo es el mismo que en producción, que
# es lo que interesa probar; el costo deliberado no aporta nada a una prueba.
TEST_HASHER = BcryptPasswordHasher(rounds=4)
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
    reservations_models,
    restaurants_models,
)

VALID_PASSWORD = "contrasena-larga"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

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
            raise LlmUnavailable("La IA no está configurada en este servidor.")
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
def decision_engine() -> DecisionEngine:
    """Las reglas fijas, como un servidor sin clave de TypeSafe.

    Ninguna prueba llama a Jev de verdad, aunque el `.env` tenga una clave. Las
    que prueban Jev lo hacen con un transporte HTTP falso.
    """
    return DecisionEngineSelector(rules=RuleBasedDecisionEngine(), jev=None, min_confidence=0.5)


@pytest.fixture
async def client(
    session: AsyncSession,
    broker: LocalBroker,
    llm: FakeLlmClient,
    jobs: BackgroundJobs,
    decision_engine: DecisionEngine,
) -> AsyncIterator[AsyncClient]:
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
    app.dependency_overrides[get_token_service] = lambda: TEST_TOKEN_SERVICE
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_background_jobs] = lambda: jobs
    app.dependency_overrides[get_decision_engine] = lambda: decision_engine
    # Cada prueba arranca sin intentos fallidos acumulados por otra.
    throttle = LoginThrottle()
    app.dependency_overrides[get_login_throttle] = lambda: throttle

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
        raise ValueError("El usuario debe estar persistido para emitirle un token.")
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


async def staffed_restaurant(session: AsyncSession, slug: str) -> StaffedRestaurant:
    restaurant = await SqlAlchemyRestaurantRepository(session).add(
        Restaurant(name=f"Restaurante {slug}", slug=slug)
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


@pytest.fixture
async def local_a(session: AsyncSession) -> StaffedRestaurant:
    return await staffed_restaurant(session, "local-a")


@pytest.fixture
async def local_b(session: AsyncSession) -> StaffedRestaurant:
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
