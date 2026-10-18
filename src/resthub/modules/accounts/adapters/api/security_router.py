"""Adaptador HTTP de la seguridad de la cuenta propia.

Las sesiones abiertas en cada dispositivo (RFS-17) y la contraseña confirmada
antes de una acción crítica (RFS-21). Sirve a las dos clases de
cuenta, del personal y de la plataforma, igual que `GET /auth/me`. Vive aparte
de `auth_router.py` (entrar, renovar, salir) para que cada archivo diga una cosa.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request, Response, status

from resthub.core.access_history import PLATFORM_ACCOUNT, STAFF_ACCOUNT
from resthub.core.account_sessions import SessionRegistryDep
from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import (
    ActiveSessionSubjectDep,
    client_address,
    reauthentication_window,
)
from resthub.core.database import SessionDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.identity import PlatformClaims, Principal
from resthub.core.login_attempts import LoginGuardDep
from resthub.core.logs import get_logger
from resthub.core.session_cookie import clear_session
from resthub.modules.accounts.adapters.api.dependencies import (
    PasswordHasherDep,
    PlatformSignInDep,
    UserRepositoryDep,
)
from resthub.modules.accounts.adapters.api.schemas import (
    DeviceSessionResponse,
    ReauthenticateRequest,
    ReauthenticationResponse,
)
from resthub.modules.accounts.domain.exceptions import (
    PreviewSessionRestrictedError,
    UserNotFoundError,
    WrongCurrentPasswordError,
)
from resthub.modules.accounts.use_cases.reauthenticate import (
    VerifyOwnPassword,
    VerifyOwnPasswordCommand,
)

router = APIRouter()
logger = get_logger("resthub.auth.security")

SessionRef = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[0-9a-f]+$")]


def session_not_found() -> ApiError:
    message = "La sesión no existe o ya se cerró."
    return ApiError(status.HTTP_404_NOT_FOUND, message, ErrorCode.SESSION_NOT_FOUND)


def account_owner(subject: Principal | PlatformClaims) -> tuple[str, int, str]:
    """De quién son las sesiones y cuál es la de esta petición."""
    if isinstance(subject, PlatformClaims):
        return PLATFORM_ACCOUNT, subject.admin_id, subject.session_id or ""
    if subject.preview or subject.session_id is None:
        message = "La vista previa no tiene sesiones propias."
        raise ApiError(status.HTTP_403_FORBIDDEN, message, ErrorCode.PREVIEW_SESSION_RESTRICTED)
    return STAFF_ACCOUNT, subject.user_id, subject.session_id


@router.get(
    "/me/sessions",
    response_model=list[DeviceSessionResponse],
    summary="Las sesiones abiertas de la cuenta propia",
)
async def list_own_sessions(
    subject: ActiveSessionSubjectDep, sessions: SessionRegistryDep, db: SessionDep
) -> list[DeviceSessionResponse]:
    """RFS-17: cada dispositivo con la sesión abierta, el usado más recientemente primero."""
    kind, account_id, current = account_owner(subject)
    opened = await sessions.list_open(db, kind, account_id)
    return [
        DeviceSessionResponse.from_session(state, current=state.id == current) for state in opened
    ]


@router.delete(
    "/me/sessions/{session_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar una sesión propia",
)
async def close_own_session(
    session_ref: SessionRef,
    subject: ActiveSessionSubjectDep,
    sessions: SessionRegistryDep,
    activity: ActivityRecorderDep,
    response: Response,
    db: SessionDep,
) -> None:
    """Cierra la sesión de un dispositivo (el celular perdido) sin tocar las demás.

    Si es la de esta petición, equivale a salir.
    """
    kind, account_id, current = account_owner(subject)
    target = await sessions.find_open_by_ref(db, kind, account_id, session_ref)
    if target is None:
        raise session_not_found()
    await sessions.revoke(db, target.id)
    if target.id == current:
        clear_session(response)
    elif isinstance(subject, Principal):
        await activity.record(
            subject.restaurant_id, subject.user_id, ActivityKind.SESSION_CLOSED, target.device
        )
    logger.info("auth.session_closed", account_kind=kind, account_id=account_id)


@router.delete(
    "/me/sessions",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar las sesiones de los demás dispositivos",
)
async def close_other_sessions(
    subject: ActiveSessionSubjectDep,
    sessions: SessionRegistryDep,
    activity: ActivityRecorderDep,
    db: SessionDep,
) -> None:
    """Deja abierta solo la sesión de esta petición."""
    kind, account_id, current = account_owner(subject)
    await sessions.revoke_all(db, kind, account_id, keep=current)
    if isinstance(subject, Principal):
        await activity.record(
            subject.restaurant_id, subject.user_id, ActivityKind.SESSION_CLOSED, "Todas las demás"
        )
    logger.info("auth.other_sessions_closed", account_kind=kind, account_id=account_id)


def throttled(wait: int) -> ApiError:
    return ApiError(
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"Demasiados intentos fallidos. Vuelve a intentar en {wait // 60 + 1} minutos.",
        ErrorCode.LOGIN_THROTTLED,
        {"Retry-After": str(wait)},
    )


async def verify_own_password(
    subject: Principal | PlatformClaims,
    password: str,
    *,
    users: UserRepositoryDep,
    hasher: PasswordHasherDep,
    platform: PlatformSignInDep,
) -> None:
    """La contraseña de la cuenta que ya tiene la sesión abierta; 400 si no es."""
    if isinstance(subject, PlatformClaims):
        if not await platform.verify_password(subject.admin_id, password):
            raise api_error(status.HTTP_400_BAD_REQUEST, WrongCurrentPasswordError())
        return
    try:
        await VerifyOwnPassword(users, hasher)(
            VerifyOwnPasswordCommand(
                user_id=subject.user_id, password=password, preview=subject.preview
            )
        )
    except PreviewSessionRestrictedError as error:
        raise api_error(status.HTTP_403_FORBIDDEN, error) from error
    except UserNotFoundError as error:
        raise api_error(status.HTTP_404_NOT_FOUND, error) from error
    except WrongCurrentPasswordError as error:
        raise api_error(status.HTTP_400_BAD_REQUEST, error) from error


@router.post(
    "/me/reauthenticate",
    response_model=ReauthenticationResponse,
    summary="Confirmar la contraseña antes de una acción crítica",
)
async def reauthenticate(
    payload: ReauthenticateRequest,
    subject: ActiveSessionSubjectDep,
    request: Request,
    throttle: LoginGuardDep,
    users: UserRepositoryDep,
    hasher: PasswordHasherDep,
    platform: PlatformSignInDep,
    sessions: SessionRegistryDep,
    db: SessionDep,
) -> ReauthenticationResponse:
    """Marca esta sesión como reautenticada (RFS-21).

    Datos fiscales, credenciales SOL, certificado, roles y permisos, y desactivar
    cuentas o locales piden la contraseña de nuevo. Vale cinco minutos y solo en
    esta sesión. Los intentos fallidos cuentan como los del acceso: tras varios
    seguidos responde 429.
    """
    kind, account_id, current = account_owner(subject)
    key, address = f"reauthenticate:{kind}:{account_id}", client_address(request)
    # La identificación pudo anotar el uso de la sesión (`last_seen_at`) en `db`, y
    # el límite de intentos escribe en una sesión propia: en SQLite esa segunda
    # escritura esperaría el candado que `db` retiene hasta el final de esta misma
    # petición y respondería 500 «database is locked». Se confirma antes.
    await db.commit()
    if wait := await throttle.acquire(key, address):
        raise throttled(wait)
    await verify_own_password(
        subject, payload.password, users=users, hasher=hasher, platform=platform
    )
    await throttle.succeeded(key, address, db)
    moment = await sessions.mark_reauthenticated(db, current)
    logger.info("auth.reauthenticated", account_kind=kind, account_id=account_id)
    return ReauthenticationResponse(
        reauthenticated_at=moment, valid_until=moment + reauthentication_window()
    )
