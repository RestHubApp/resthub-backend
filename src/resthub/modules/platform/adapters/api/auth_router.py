"""Acceso de la administración del sistema.

Separado del acceso del personal: otra tabla, otro alcance en el token y otro
contador de intentos. Deja el mismo rastro de seguridad en los logs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from resthub.core.auth import UNAUTHENTICATED_HEADERS, PlatformTokenServiceDep, client_address
from resthub.core.logs import get_logger, mask_email
from resthub.modules.platform.adapters.api.dependencies import (
    AdminRepositoryDep,
    CurrentAdminDep,
    PasswordHasherDep,
    PlatformActivityLogDep,
    PlatformThrottleDep,
)
from resthub.modules.platform.adapters.api.schemas import (
    PlatformAccessTokenResponse,
    PlatformAdminResponse,
    PlatformLoginRequest,
    PlatformSessionResponse,
)
from resthub.modules.platform.domain.exceptions import InvalidCredentials
from resthub.modules.platform.use_cases.manage_admins import (
    AuthenticateAdmin,
    AuthenticateAdminCommand,
)

router = APIRouter()
logger = get_logger("resthub.platform.auth")


@router.post(
    "/login",
    response_model=PlatformAccessTokenResponse,
    summary="Obtener un token de la administración del sistema",
)
async def login(
    payload: PlatformLoginRequest,
    request: Request,
    throttle: PlatformThrottleDep,
    admins: AdminRepositoryDep,
    hasher: PasswordHasherDep,
    tokens: PlatformTokenServiceDep,
    activity: PlatformActivityLogDep,
) -> PlatformAccessTokenResponse:
    email, address = str(payload.email), client_address(request)
    wait = throttle.retry_after(email, address)
    if wait:
        logger.warning("platform.login_throttled", email=mask_email(email), retry_after=wait)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Demasiados intentos fallidos. Vuelve a intentar en {wait // 60 + 1} minutos.",
            headers={"Retry-After": str(wait)},
        )
    try:
        result = await AuthenticateAdmin(admins, hasher, tokens, activity)(
            AuthenticateAdminCommand(email=email, password=payload.password)
        )
    except InvalidCredentials as error:
        throttle.failed(email, address)
        logger.warning("platform.login_failed", email=mask_email(email))
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(error), headers=UNAUTHENTICATED_HEADERS
        ) from error

    throttle.succeeded(email, address)
    logger.info("platform.login_succeeded", admin_id=result.admin.id)
    return PlatformAccessTokenResponse(
        admin=PlatformAdminResponse.from_entity(result.admin),
        access_token=result.token.value,
        expires_in=result.token.expires_in_seconds,
    )


@router.post(
    "/refresh",
    response_model=PlatformAccessTokenResponse,
    summary="Renovar el token de la administración del sistema",
)
async def refresh_token(
    admin: CurrentAdminDep, tokens: PlatformTokenServiceDep
) -> PlatformAccessTokenResponse:
    # Una cuenta desactivada no llega acá: la dependencia ya la releyó.
    token = tokens.issue_platform(admin.id or 0)
    return PlatformAccessTokenResponse(
        admin=PlatformAdminResponse.from_entity(admin),
        access_token=token.value,
        expires_in=token.expires_in_seconds,
    )


@router.get(
    "/me",
    response_model=PlatformSessionResponse,
    summary="Cuenta de plataforma que pregunta",
)
async def read_current_admin(admin: CurrentAdminDep) -> PlatformSessionResponse:
    return PlatformSessionResponse(admin=PlatformAdminResponse.from_entity(admin))
