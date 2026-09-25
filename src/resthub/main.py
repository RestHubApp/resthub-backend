"""Ensamblado de la aplicación.

Este es el único lugar donde los módulos de dominio se conocen entre sí, y solo
para montar sus routers. Ningún módulo importa a otro.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from resthub.core.config import get_settings
from resthub.core.database import engine
from resthub.core.events_router import router as events_router
from resthub.core.logs import configure_logging, get_logger
from resthub.core.realtime_broker import get_broker
from resthub.core.request_logging import REQUEST_ID_HEADER, RequestLoggingMiddleware
from resthub.modules.accounts.adapters.api.activity_router import router as activity_router
from resthub.modules.accounts.adapters.api.auth_router import router as auth_router
from resthub.modules.accounts.adapters.api.staff_router import router as staff_router
from resthub.modules.menu.adapters.api.router import router as menu_router
from resthub.modules.orders.adapters.api.orders_router import router as orders_router
from resthub.modules.orders.adapters.api.tables_router import router as tables_router
from resthub.modules.restaurants.adapters.api.router import router as restaurant_router

API_PREFIX = "/api/v1"

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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # El esquema lo crean las migraciones de Alembic, también en desarrollo.
    # Crearlo al arrancar dejaría que la base local se apartara del historial
    # de migraciones sin que nadie se enterara hasta el despliegue.
    broker = get_broker()
    await broker.start()
    logger.info("app.started", version=settings.app_version, debug=settings.debug)
    yield
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
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Sin exponerla, el navegador no deja leer la cabecera desde otro
        # origen y el frontend no podría anotar el identificador de un error.
        expose_headers=[REQUEST_ID_HEADER],
    )
    # Se agrega al final para quedar por fuera de CORS: así también se
    # registran las respuestas que CORS corta antes de llegar a un router.
    app.add_middleware(RequestLoggingMiddleware)

    @app.get(f"{API_PREFIX}/health", tags=["system"], summary="Sondeo de vida")
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=settings.app_name,
            version=settings.app_version,
        )

    app.include_router(events_router, prefix=f"{API_PREFIX}/events", tags=["system"])
    app.include_router(auth_router, prefix=f"{API_PREFIX}/auth", tags=["auth"])
    app.include_router(restaurant_router, prefix=f"{API_PREFIX}/restaurant", tags=["restaurant"])
    app.include_router(staff_router, prefix=f"{API_PREFIX}/staff", tags=["staff"])
    app.include_router(activity_router, prefix=f"{API_PREFIX}/activity", tags=["activity"])
    app.include_router(menu_router, prefix=f"{API_PREFIX}/menu", tags=["menu"])
    app.include_router(tables_router, prefix=f"{API_PREFIX}/tables", tags=["tables"])
    app.include_router(orders_router, prefix=f"{API_PREFIX}/orders", tags=["orders"])
    return app


app = create_app()
