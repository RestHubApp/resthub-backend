"""Cableado del adaptador HTTP de la plataforma y su propia autenticación.

La autenticación de plataforma vive acá y no en `core/auth.py`: solo la usa
este módulo, y la tabla que lee (`platform_admins`) es suya. Del núcleo toma lo
compartido: el servicio de tokens, que es el que distingue el alcance, y el
límite de intentos.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from resthub.core.access_history import PLATFORM_ACCOUNT
from resthub.core.account_sessions import AccountSession, SessionRegistryDep
from resthub.core.auth import (
    PlatformTokenServiceDep,
    credentials_from,
    ensure_open_session,
    unauthenticated,
)
from resthub.core.config import get_settings
from resthub.core.database import SessionDep
from resthub.core.errors import api_error
from resthub.core.identity import InvalidTokenError, MissingPermissionError
from resthub.core.permissions import PlatformPermission, platform_permissions
from resthub.core.request_context import PLATFORM, annotate_account
from resthub.core.security import hasher_from_settings
from resthub.core.session_cookie import PLATFORM_COOKIE
from resthub.core.two_factor import (
    PlatformTwoFactorPolicyDep,
    TwoFactorStoreDep,
    two_factor_setup_required,
)
from resthub.modules.platform.adapters.persistence.directories import (
    SqlAlchemyRestaurantCatalogDirectory,
    SqlAlchemySandboxCatalogDirectory,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_activity_log import (
    SqlAlchemyPlatformActivityLogRepository,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_security_alerts import (
    SqlAlchemySecurityAlertRepository,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_telemetry_reader import (
    SqlAlchemyTelemetryReader,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.exceptions import AdminUnavailableError
from resthub.modules.platform.ports.activity_log import PlatformActivityLogRepository
from resthub.modules.platform.ports.admin_repository import (
    PasswordHasher,
    PlatformAdminRepository,
)
from resthub.modules.platform.ports.observability import TelemetryReader
from resthub.modules.platform.ports.restaurants import (
    RestaurantCatalogDirectory,
    RestaurantProvisioningGateway,
)
from resthub.modules.platform.ports.sandbox import (
    SandboxCatalogDirectory,
    SandboxProvisioningGateway,
)
from resthub.modules.platform.ports.security_alerts import SecurityAlertRepository
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


def get_platform_activity_log(session: SessionDep) -> PlatformActivityLogRepository:
    return SqlAlchemyPlatformActivityLogRepository(session)


def get_restaurant_catalog(session: SessionDep) -> RestaurantCatalogDirectory:
    return SqlAlchemyRestaurantCatalogDirectory(session)


def get_sandbox_catalog(session: SessionDep) -> SandboxCatalogDirectory:
    return SqlAlchemySandboxCatalogDirectory(session)


def get_telemetry_reader(session: SessionDep) -> TelemetryReader:
    return SqlAlchemyTelemetryReader(session)


def get_security_alerts(session: SessionDep) -> SecurityAlertRepository:
    return SqlAlchemySecurityAlertRepository(session)


def get_password_hasher() -> PasswordHasher:
    return hasher_from_settings(get_settings())


def get_restaurant_provisioning() -> RestaurantProvisioningGateway:
    """Sin implementación propia: escribir en `restaurants` y `accounts` no es de este módulo.

    `main.py` la reemplaza por la de `wiring/restaurant_provisioning.py`.
    """
    message = "El alta de restaurantes no está conectada a la aplicación."
    raise NotImplementedError(message)


def get_sandbox_provisioning() -> SandboxProvisioningGateway:
    """Sin implementación propia: el local de muestra y sus códigos son de otros módulos.

    `main.py` la reemplaza por la de `wiring/sandbox.py`.
    """
    message = "El local de muestra no está conectado a la aplicación."
    raise NotImplementedError(message)


AdminRepositoryDep = Annotated[PlatformAdminRepository, Depends(get_admin_repository)]
PlatformActivityLogDep = Annotated[
    PlatformActivityLogRepository, Depends(get_platform_activity_log)
]
RestaurantCatalogDep = Annotated[RestaurantCatalogDirectory, Depends(get_restaurant_catalog)]
PasswordHasherDep = Annotated[PasswordHasher, Depends(get_password_hasher)]
RestaurantProvisioningDep = Annotated[
    RestaurantProvisioningGateway, Depends(get_restaurant_provisioning)
]
SandboxCatalogDep = Annotated[SandboxCatalogDirectory, Depends(get_sandbox_catalog)]
SandboxProvisioningDep = Annotated[SandboxProvisioningGateway, Depends(get_sandbox_provisioning)]
TelemetryReaderDep = Annotated[TelemetryReader, Depends(get_telemetry_reader)]
SecurityAlertsDep = Annotated[SecurityAlertRepository, Depends(get_security_alerts)]


async def get_platform_credentials(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(platform_bearer)],
) -> HTTPAuthorizationCredentials | None:
    """Toma la credencial de `Authorization: Bearer` o de la cookie de plataforma (con CSRF)."""
    return credentials_from(request, bearer, PLATFORM_COOKIE)


PlatformCredentialsDep = Annotated[
    HTTPAuthorizationCredentials | None, Depends(get_platform_credentials)
]


@dataclass(frozen=True, slots=True)
class AdminSession:
    """La cuenta de plataforma que pregunta y la sesión de su dispositivo."""

    admin: PlatformAdmin
    session: AccountSession


async def get_admin_session(
    credentials: PlatformCredentialsDep,
    admins: AdminRepositoryDep,
    tokens: PlatformTokenServiceDep,
    registry: SessionRegistryDep,
    session: SessionDep,
) -> AdminSession:
    if credentials is None:
        message = "Falta la credencial de acceso."
        raise unauthenticated(message)
    try:
        # Rechaza cualquier token de restaurante, con o sin alcance.
        claims = tokens.decode_platform(credentials.credentials)
    except InvalidTokenError as error:
        raise unauthenticated(str(error), error.code) from error
    try:
        admin = await ReadCurrentAdmin(admins)(claims.admin_id, claims.issued_at)
    except AdminUnavailableError as error:
        raise unauthenticated(str(error), error.code) from error
    state = await ensure_open_session(
        registry,
        session,
        claims.session_id,
        account_kind=PLATFORM_ACCOUNT,
        account_id=claims.admin_id,
    )
    # Para la telemetría del panel de observabilidad: quién hizo la petición.
    annotate_account(PLATFORM, account_id=admin.id or claims.admin_id)
    return AdminSession(admin=admin, session=state)


AdminSessionDep = Annotated[AdminSession, Depends(get_admin_session)]


async def get_current_admin(current: AdminSessionDep) -> PlatformAdmin:
    return current.admin


CurrentAdminDep = Annotated[PlatformAdmin, Depends(get_current_admin)]


async def get_second_factor_admin(
    admin: CurrentAdminDep,
    store: TwoFactorStoreDep,
    required: PlatformTwoFactorPolicyDep,
    session: SessionDep,
) -> PlatformAdmin:
    """La cuenta de plataforma, solo si ya activó la verificación en dos pasos (RFS-22).

    Sin ella, la contraseña sola abre el alta de restaurantes y la vista previa
    de cualquier local: 403 hasta activarla. Activarla (`/auth/me/two-factor`)
    no pasa por acá.
    """
    if required and not await store.is_enabled(session, PLATFORM_ACCOUNT, admin.id or 0):
        raise two_factor_setup_required()
    return admin


SecondFactorAdminDep = Annotated[PlatformAdmin, Depends(get_second_factor_admin)]


async def get_admin_permissions(_: CurrentAdminDep) -> frozenset[PlatformPermission]:
    """Lo que la cuenta que pregunta puede hacer; las pruebas lo reemplazan para recortarlo."""
    return platform_permissions()


AdminPermissionsDep = Annotated[frozenset[PlatformPermission], Depends(get_admin_permissions)]


def require_platform_permission(
    *required: PlatformPermission,
) -> Callable[[PlatformAdmin, frozenset[PlatformPermission]], Awaitable[PlatformAdmin]]:
    """Exige al menos uno de los permisos de plataforma pedidos.

    Cada endpoint declara la acción que hace, no quién puede hacerla. Hoy toda
    cuenta de plataforma tiene el catálogo entero (`platform_permissions`), así
    que la comprobación solo rechaza a quien no sea de plataforma —eso ya lo
    hace `CurrentAdminDep`— o, cuando existan roles de plataforma, a quien no
    tenga el permiso: 403 con el mismo código que el del personal.
    """

    async def dependency(
        admin: SecondFactorAdminDep, granted: AdminPermissionsDep
    ) -> PlatformAdmin:
        if not any(permission in granted for permission in required):
            raise api_error(
                status.HTTP_403_FORBIDDEN,
                MissingPermissionError(tuple(permission.value for permission in required)),
            )
        return admin

    return dependency


RestaurantsReaderDep = Annotated[
    PlatformAdmin, Depends(require_platform_permission(PlatformPermission.RESTAURANTS_READ))
]
RestaurantsManagerDep = Annotated[
    PlatformAdmin, Depends(require_platform_permission(PlatformPermission.RESTAURANTS_MANAGE))
]
ActivityReaderDep = Annotated[
    PlatformAdmin, Depends(require_platform_permission(PlatformPermission.ACTIVITY_READ))
]
PreviewOpenerDep = Annotated[
    PlatformAdmin, Depends(require_platform_permission(PlatformPermission.PREVIEW_OPEN))
]
ObservabilityReaderDep = Annotated[
    PlatformAdmin, Depends(require_platform_permission(PlatformPermission.OBSERVABILITY_READ))
]
