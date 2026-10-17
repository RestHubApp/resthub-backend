"""Acceso de la administración del sistema.

Obsoleto: el acceso es único (`POST /auth/login`, que atiende también a las
cuentas de plataforma). Estas rutas siguen funcionando hasta que ninguna
interfaz las use y se borran en un cambio posterior. Mantienen su propio
contador de intentos y el mismo rastro de seguridad en los logs.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from resthub.core.access_history import (
    PLATFORM_ACCOUNT,
    AccessHistoryDep,
    AccessOutcome,
    LoginAttempt,
)
from resthub.core.account_sessions import SessionOrigin, SessionRegistryDep
from resthub.core.auth import UNAUTHENTICATED_HEADERS, PlatformTokenServiceDep, client_address
from resthub.core.database import SessionDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.login_attempts import PlatformLoginGuardDep
from resthub.core.logs import get_logger, mask_email
from resthub.core.two_factor import TwoFactorStoreDep, check_second_factor
from resthub.modules.platform.adapters.api.dependencies import (
    AdminRepositoryDep,
    AdminSessionDep,
    CurrentAdminDep,
    PasswordHasherDep,
    PlatformActivityLogDep,
)
from resthub.modules.platform.adapters.api.schemas import (
    PlatformAccessTokenResponse,
    PlatformAdminResponse,
    PlatformLoginRequest,
    PlatformSessionResponse,
)
from resthub.modules.platform.domain.exceptions import InvalidCredentialsError
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
    deprecated=True,
)
async def login(
    payload: PlatformLoginRequest,
    request: Request,
    throttle: PlatformLoginGuardDep,
    admins: AdminRepositoryDep,
    hasher: PasswordHasherDep,
    tokens: PlatformTokenServiceDep,
    activity: PlatformActivityLogDep,
    history: AccessHistoryDep,
    sessions: SessionRegistryDep,
    two_factor: TwoFactorStoreDep,
    session: SessionDep,
) -> PlatformAccessTokenResponse:
    email, address = str(payload.email), client_address(request)
    agent = request.headers.get("user-agent", "")
    # Como en el acceso del personal: el intento se cuenta antes de verificar.
    wait = await throttle.acquire(email, address)
    if wait:
        await history.record(LoginAttempt(email, AccessOutcome.THROTTLED, address, agent))
        logger.warning("platform.login_throttled", email=mask_email(email), retry_after=wait)
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Demasiados intentos fallidos. Vuelve a intentar en {wait // 60 + 1} minutos.",
            ErrorCode.LOGIN_THROTTLED,
            {"Retry-After": str(wait)},
        )
    try:
        result = await AuthenticateAdmin(admins, hasher, tokens, activity)(
            AuthenticateAdminCommand(email=email, password=payload.password)
        )
    except InvalidCredentialsError as error:
        await history.record(LoginAttempt(email, AccessOutcome.FAILED, address, agent))
        logger.warning("platform.login_failed", email=mask_email(email))
        raise api_error(
            status.HTTP_401_UNAUTHORIZED, error, headers=UNAUTHENTICATED_HEADERS
        ) from error

    await check_second_factor(
        two_factor,
        session,
        (PLATFORM_ACCOUNT, result.admin.id or 0),
        payload.otp_code,
        on_failure=lambda: history.record(
            LoginAttempt(email, AccessOutcome.FAILED, address, agent)
        ),
    )
    await throttle.succeeded(email, address, session)
    await history.record(LoginAttempt(email, AccessOutcome.SUCCEEDED, address, agent), session)
    await sessions.open(
        session,
        session_id=result.token.session_id,
        account_kind=PLATFORM_ACCOUNT,
        account_id=result.admin.id or 0,
        restaurant_id=None,
        origin=SessionOrigin(address, agent),
    )
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
    deprecated=True,
)
async def refresh_token(
    current: AdminSessionDep, tokens: PlatformTokenServiceDep
) -> PlatformAccessTokenResponse:
    # Una cuenta desactivada no llega acá: la dependencia ya la releyó.
    admin = current.admin
    token = tokens.issue_platform(admin.id or 0, session_id=current.session.id)
    return PlatformAccessTokenResponse(
        admin=PlatformAdminResponse.from_entity(admin),
        access_token=token.value,
        expires_in=token.expires_in_seconds,
    )


@router.get(
    "/me",
    response_model=PlatformSessionResponse,
    summary="Cuenta de plataforma que pregunta",
    deprecated=True,
)
async def read_current_admin(admin: CurrentAdminDep) -> PlatformSessionResponse:
    return PlatformSessionResponse(admin=PlatformAdminResponse.from_entity(admin))
