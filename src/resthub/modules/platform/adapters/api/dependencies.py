"""Cableado del adaptador HTTP de la plataforma y su propia autenticación.

La autenticación de plataforma vive acá y no en `core/auth.py`: solo la usa
este módulo, y la tabla que lee (`platform_admins`) es suya. Del núcleo toma lo
compartido: el servicio de tokens, que es el que distingue el alcance, y el
límite de intentos.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from resthub.core.auth import PlatformTokenServiceDep, SessionDep, unauthenticated
from resthub.core.identity import InvalidToken
from resthub.core.login_throttle import LoginThrottle, get_platform_login_throttle
from resthub.core.security import BcryptPasswordHasher
from resthub.modules.platform.adapters.persistence.directories import SqlRestaurantCatalog
from resthub.modules.platform.adapters.persistence.sqlalchemy_activity_log import (
    SqlAlchemyPlatformActivityLog,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.exceptions import AdminUnavailable
from resthub.modules.platform.ports.activity_log import PlatformActivityLog
from resthub.modules.platform.ports.admin_repository import (
    PasswordHasher,
    PlatformAdminRepository,
)
from resthub.modules.platform.ports.restaurants import RestaurantCatalog, RestaurantProvisioning
from resthub.modules.platform.use_cases.manage_admins import ReadCurrentAdmin

# Esquema aparte del del personal para que la documentación diga de dónde sale
# el token; en el cable es el mismo `Authorization: Bearer`.
platform_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="PlatformBearer",
    description="Token emitido por /api/v1/platform/auth/login",
)


def get_admin_repository(session: SessionDep) -> PlatformAdminRepository:
    return SqlAlchemyPlatformAdminRepository(session)


def get_platform_activity_log(session: SessionDep) -> PlatformActivityLog:
    return SqlAlchemyPlatformActivityLog(session)


def get_restaurant_catalog(session: SessionDep) -> RestaurantCatalog:
    return SqlRestaurantCatalog(session)


def get_password_hasher() -> PasswordHasher:
    return BcryptPasswordHasher()


def get_restaurant_provisioning() -> RestaurantProvisioning:
    """Sin implementación propia: escribir en `restaurants` y `accounts` no es de este módulo.

    `main.py` la reemplaza por la de `wiring/restaurant_provisioning.py`.
    """
    raise NotImplementedError("El alta de restaurantes no está conectada a la aplicación.")


AdminRepositoryDep = Annotated[PlatformAdminRepository, Depends(get_admin_repository)]
PlatformActivityLogDep = Annotated[PlatformActivityLog, Depends(get_platform_activity_log)]
RestaurantCatalogDep = Annotated[RestaurantCatalog, Depends(get_restaurant_catalog)]
PasswordHasherDep = Annotated[PasswordHasher, Depends(get_password_hasher)]
RestaurantProvisioningDep = Annotated[RestaurantProvisioning, Depends(get_restaurant_provisioning)]
PlatformThrottleDep = Annotated[LoginThrottle, Depends(get_platform_login_throttle)]
PlatformCredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(platform_bearer)]


async def get_current_admin(
    credentials: PlatformCredentialsDep,
    admins: AdminRepositoryDep,
    tokens: PlatformTokenServiceDep,
) -> PlatformAdmin:
    if credentials is None:
        raise unauthenticated("Falta la credencial de acceso.")
    try:
        # Rechaza cualquier token de restaurante, con o sin alcance.
        claims = tokens.decode_platform(credentials.credentials)
    except InvalidToken as error:
        raise unauthenticated(str(error)) from error
    try:
        return await ReadCurrentAdmin(admins)(claims.admin_id)
    except AdminUnavailable as error:
        raise unauthenticated(str(error)) from error


CurrentAdminDep = Annotated[PlatformAdmin, Depends(get_current_admin)]
