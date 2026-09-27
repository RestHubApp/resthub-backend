"""Ensamblado de la aplicación.

Este es el único lugar donde los módulos de dominio se conocen entre sí: monta
sus routers y conecta los puertos que un módulo declara con lo que otro ofrece
(ver `resthub.wiring`). Ningún módulo importa a otro.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from resthub.core.background import get_background_jobs
from resthub.core.config import get_settings
from resthub.core.database import SessionDep, engine, get_session_factory
from resthub.core.database_errors import install_database_error_handlers
from resthub.core.events_router import router as events_router
from resthub.core.logs import configure_logging, get_logger
from resthub.core.realtime_broker import get_broker
from resthub.core.request_logging import REQUEST_ID_HEADER, RequestLoggingMiddleware
from resthub.core.telemetry import get_telemetry
from resthub.modules.accounts.adapters.api.activity_router import router as activity_router
from resthub.modules.accounts.adapters.api.auth_router import router as auth_router
from resthub.modules.accounts.adapters.api.roles_router import permissions_router
from resthub.modules.accounts.adapters.api.roles_router import router as roles_router
from resthub.modules.accounts.adapters.api.staff_router import router as staff_router
from resthub.modules.billing.adapters.api.router import router as billing_router
from resthub.modules.customers.adapters.api.router import router as customers_router
from resthub.modules.insights.adapters.api.router import router as insights_router
from resthub.modules.inventory.adapters.api.purchasing_router import router as purchasing_router
from resthub.modules.inventory.adapters.api.router import router as inventory_router
from resthub.modules.menu.adapters.api.router import router as menu_router
from resthub.modules.orders.adapters.api.cash_router import router as cash_router
from resthub.modules.orders.adapters.api.dependencies import (
    get_sent_to_kitchen_hook,
    get_served_order_hook,
)
from resthub.modules.orders.adapters.api.orders_router import router as orders_router
from resthub.modules.orders.adapters.api.tables_router import router as tables_router
from resthub.modules.platform.adapters.api.activity_router import (
    router as platform_activity_router,
)
from resthub.modules.platform.adapters.api.auth_router import router as platform_auth_router
from resthub.modules.platform.adapters.api.dependencies import (
    get_restaurant_provisioning,
    get_sandbox_provisioning,
)
from resthub.modules.platform.adapters.api.observability_router import (
    router as platform_observability_router,
)
from resthub.modules.platform.adapters.api.restaurants_router import (
    router as platform_restaurants_router,
)
from resthub.modules.platform.adapters.api.sandbox_router import (
    preview_router as platform_preview_router,
)
from resthub.modules.platform.adapters.api.sandbox_router import (
    router as platform_sandbox_router,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_telemetry_sink import SqlTelemetrySink
from resthub.modules.reservations.adapters.api.router import router as reservations_router
from resthub.modules.restaurants.adapters.api.router import router as restaurant_router
from resthub.wiring.kitchen_consumption import get_inventory_consumption
from resthub.wiring.kitchen_notes import get_kitchen_note_classification
from resthub.wiring.restaurant_provisioning import (
    get_restaurant_provisioning as get_module_restaurant_provisioning,
)
from resthub.wiring.sandbox import get_sandbox_provisioning as get_module_sandbox_provisioning

API_PREFIX = "/api/v1"
# Rutas que no se guardan en la telemetría, ni ellas ni sus eventos: el sondeo
# de vida (cada pocos segundos), la conexión de avisos (abierta por horas) y el
# propio panel (mirarlo no tiene que llenarlo).
UNTRACKED_PATHS = (
    f"{API_PREFIX}/health",
    f"{API_PREFIX}/events",
    f"{API_PREFIX}/platform/observability",
)

# Cuánto espera el sondeo de vida a la base antes de darla por caída. Corto a
# propósito: el sondeo tiene que contestar aunque la base no conteste.
HEALTH_DATABASE_TIMEOUT_SECONDS = 2.0

settings = get_settings()
logger = get_logger("resthub.app")


class HealthResponse(BaseModel):
    """Respuesta del sondeo de vida.

    Está tipada, y no devuelta como diccionario suelto, para que el esquema
    OpenAPI describa los campos y el frontend derive el tipo exacto.
    """

    status: str
    service: str
    version: str
    # `ok` o `unavailable`. Con la base caída el sondeo responde 503: el
    # proceso está vivo, pero no puede atender pedidos.
    database: str


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # El esquema lo crean las migraciones de Alembic, también en desarrollo.
    # Crearlo al arrancar dejaría que la base local se apartara del historial
    # de migraciones sin que nadie se enterara hasta el despliegue.
    broker = get_broker()
    await broker.start()
    # La telemetría del panel de observabilidad se guarda en la propia base.
    # El sumidero es de `platform`, que es quien la consulta; el núcleo solo
    # conoce su puerto. Apagada (`OBSERVABILITY_ENABLED=false`), no se instala
    # y la captura no hace nada.
    telemetry = get_telemetry()
    if telemetry.enabled:
        telemetry.install(SqlTelemetrySink(get_session_factory()))
        await telemetry.start()
    logger.info("app.started", version=settings.app_version, debug=settings.debug)
    yield
    # Lo que quedó corriendo en segundo plano (clasificar notas con la IA)
    # tiene unos segundos para terminar antes de cortar la base.
    await get_background_jobs().shutdown()
    # Escribe lo que quedó en la cola antes de cerrar la base.
    await telemetry.stop()
    await broker.stop()
    await engine.dispose()
    logger.info("app.stopped")


def create_app() -> FastAPI:
    # Se configura acá y no al importar el módulo: uvicorn instala sus propios
    # handlers antes de cargar la aplicación, y esta llamada los reemplaza.
    configure_logging(level=settings.log_level, json=settings.log_json)

    app = FastAPI(
        title="RestHub API",
        version=settings.app_version,
        description="API modular para la gestión de restaurantes pequeños.",
        lifespan=lifespan,
        # El esquema y la documentación cuelgan del prefijo versionado para
        # que el proxy del frontend, que solo reenvía `/api`, los alcance.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_origin_regex=settings.cors_allowed_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Sin exponerla, el navegador no deja leer la cabecera desde otro
        # origen y el frontend no podría anotar el identificador de un error.
        expose_headers=[REQUEST_ID_HEADER],
    )
    # Una base caída responde 503 en JSON, no un 500 genérico.
    install_database_error_handlers(app)
    # Se agrega al final para quedar por fuera de CORS: así también se
    # registran las respuestas que CORS corta antes de llegar a un router.
    app.add_middleware(RequestLoggingMiddleware, untracked_paths=UNTRACKED_PATHS)

    @app.get(
        f"{API_PREFIX}/health",
        tags=["system"],
        summary="Sondeo de vida",
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    )
    async def health(session: SessionDep, response: Response) -> HealthResponse:
        # Sin mirar la base, el sondeo decía «ok» con PostgreSQL caído y todos
        # los pedidos fallando (experimento de caos 01).
        try:
            async with asyncio.timeout(HEALTH_DATABASE_TIMEOUT_SECONDS):
                await session.execute(text("SELECT 1"))
            database = "ok"
        except (TimeoutError, OSError, SQLAlchemyError):
            database = "unavailable"
            # La transacción quedó inválida: sin deshacerla, confirmarla al
            # cerrar la sesión fallaría y el sondeo respondería un 500.
            with suppress(TimeoutError, OSError, SQLAlchemyError):
                await session.rollback()
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="ok" if database == "ok" else "degraded",
            service=settings.app_name,
            version=settings.app_version,
            database=database,
        )

    app.include_router(events_router, prefix=f"{API_PREFIX}/events", tags=["system"])
    app.include_router(auth_router, prefix=f"{API_PREFIX}/auth", tags=["auth"])
    app.include_router(restaurant_router, prefix=f"{API_PREFIX}/restaurant", tags=["restaurant"])
    app.include_router(staff_router, prefix=f"{API_PREFIX}/staff", tags=["staff"])
    app.include_router(roles_router, prefix=f"{API_PREFIX}/roles", tags=["roles"])
    app.include_router(permissions_router, prefix=f"{API_PREFIX}/permissions", tags=["roles"])
    app.include_router(activity_router, prefix=f"{API_PREFIX}/activity", tags=["activity"])
    app.include_router(menu_router, prefix=f"{API_PREFIX}/menu", tags=["menu"])
    app.include_router(tables_router, prefix=f"{API_PREFIX}/tables", tags=["tables"])
    app.include_router(orders_router, prefix=f"{API_PREFIX}/orders", tags=["orders"])
    app.include_router(cash_router, prefix=f"{API_PREFIX}/cash", tags=["cash"])
    app.include_router(inventory_router, prefix=f"{API_PREFIX}/inventory", tags=["inventory"])
    app.include_router(purchasing_router, prefix=f"{API_PREFIX}/inventory", tags=["purchasing"])
    app.include_router(insights_router, prefix=f"{API_PREFIX}/insights", tags=["insights"])
    app.include_router(billing_router, prefix=f"{API_PREFIX}/billing", tags=["billing"])
    app.include_router(customers_router, prefix=f"{API_PREFIX}/customers", tags=["customers"])
    app.include_router(
        reservations_router, prefix=f"{API_PREFIX}/reservations", tags=["reservations"]
    )
    # La administración del sistema: otra credencial, otro alcance. Un token
    # de restaurante no pasa de acá, ni uno de plataforma de las rutas de arriba.
    app.include_router(
        platform_auth_router, prefix=f"{API_PREFIX}/platform/auth", tags=["platform"]
    )
    app.include_router(
        platform_restaurants_router,
        prefix=f"{API_PREFIX}/platform/restaurants",
        tags=["platform"],
    )
    app.include_router(
        platform_activity_router, prefix=f"{API_PREFIX}/platform/activity", tags=["platform"]
    )
    # La vista previa: solo entra al local de muestra, nunca a uno real.
    app.include_router(
        platform_sandbox_router, prefix=f"{API_PREFIX}/platform/sandbox", tags=["platform"]
    )
    app.include_router(
        platform_preview_router, prefix=f"{API_PREFIX}/platform/preview", tags=["platform"]
    )
    # Cómo anda la aplicación: tráfico, errores, latencias y logs.
    app.include_router(
        platform_observability_router,
        prefix=f"{API_PREFIX}/platform/observability",
        tags=["platform"],
    )

    # `orders` declara qué avisa al servir un pedido pero no quién escucha; su
    # dependencia por omisión no hace nada. Acá se reemplaza por el consumo de
    # insumos del inventario. Es el mecanismo de inyección de FastAPI usado
    # para lo que es: elegir la implementación de un puerto al ensamblar.
    app.dependency_overrides[get_served_order_hook] = get_inventory_consumption
    # Igual con el aviso de platos que llegan a cocina: lo escucha la
    # clasificación de notas de `insights`, que busca alergias sin demorar al
    # mesero (corre después de responder).
    app.dependency_overrides[get_sent_to_kitchen_hook] = get_kitchen_note_classification
    # `platform` pide dar de alta y editar restaurantes y encargados, que son
    # de `restaurants` y `accounts`; lo hacen sus casos de uso.
    app.dependency_overrides[get_restaurant_provisioning] = get_module_restaurant_provisioning
    # Igual con el local de muestra y los códigos de vista previa, que son de
    # `restaurants` y `accounts`.
    app.dependency_overrides[get_sandbox_provisioning] = get_module_sandbox_provisioning
    return app


app = create_app()
