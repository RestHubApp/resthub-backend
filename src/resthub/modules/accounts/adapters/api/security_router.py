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
    PlatformTokenServiceDep,
    TokenServiceDep,
    client_address,
    ensure_recently_authenticated,
    reauthentication_window,
)
from resthub.core.clock_dependency import ClockDep
from resthub.core.database import SessionDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.identity import AccessToken, PlatformClaims, Principal
from resthub.core.login_attempts import LoginGuardDep
from resthub.core.logs import get_logger
from resthub.core.session_cookie import (
    PLATFORM_COOKIE,
    STAFF_COOKIE,
    attach_session,
    clear_session,
)
from resthub.core.two_factor import SecondFactor, TwoFactorStoreDep, check_second_factor
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


async def ensure_subject_reauthenticated(
    subject: Principal | PlatformClaims,
    sessions: SessionRegistryDep,
    db: SessionDep,
    clock: ClockDep,
) -> None:
    """403 `reauthentication_required` si esta sesión no confirmó la contraseña hace poco.

    Sirve a las dos clases de cuenta: el principal del personal ya trae la marca;
    la de plataforma se lee de su sesión.
    """
    if isinstance(subject, Principal):
        ensure_recently_authenticated(subject.reauthenticated_at, clock.now())
        return
    state = await sessions.find(db, subject.session_id or "")
    ensure_recently_authenticated(state.reauthenticated_at if state else None, clock.now())


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
    clock: ClockDep,
    response: Response,
    db: SessionDep,
) -> None:
    """Cierra la sesión de un dispositivo (el celular perdido) sin tocar las demás.

    Cerrar la de otro dispositivo pide la contraseña confirmada hace poco (403
    `reauthentication_required`, ASVS V7.5.2): quien encuentra una sesión
    abierta no echa al dueño de las suyas. Si es la de esta petición, equivale
    a salir y no la pide.
    """
    kind, account_id, current = account_owner(subject)
    target = await sessions.find_open_by_ref(db, kind, account_id, session_ref)
    if target is None:
        raise session_not_found()
    if target.id != current:
        await ensure_subject_reauthenticated(subject, sessions, db, clock)
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
    clock: ClockDep,
    db: SessionDep,
) -> None:
    """Deja abierta solo la sesión de esta petición.

    Pide la contraseña confirmada hace poco (403 `reauthentication_required`,
    ASVS V7.5.2), como las demás acciones críticas.
    """
    kind, account_id, current = account_owner(subject)
    await ensure_subject_reauthenticated(subject, sessions, db, clock)
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


async def rotate_session_token(
    subject: Principal | PlatformClaims,
    current: str,
    response: Response,
    *,
    sessions: SessionRegistryDep,
    db: SessionDep,
    tokens: TokenServiceDep,
    platform_tokens: PlatformTokenServiceDep,
) -> tuple[AccessToken, str]:
    """Da a la sesión un `sid` nuevo, con su token y su cookie; el anterior deja de valer.

    Devuelve el token y el valor anti-CSRF de la cookie nueva (ASVS V7.2.4).
    """
    state = await sessions.find(db, current)
    if state is None:
        raise session_not_found()
    new_id = await sessions.rotate(db, state)
    if isinstance(subject, PlatformClaims):
        token = platform_tokens.issue_platform(subject.admin_id, session_id=new_id)
        cookie = PLATFORM_COOKIE
    else:
        token = tokens.issue(subject.user_id, subject.restaurant_id, session_id=new_id)
        cookie = STAFF_COOKIE
    csrf = attach_session(response, cookie, token.value, token.expires_in_seconds)
    return token, csrf


async def _nothing() -> None:
    """Un código equivocado ya cuenta en el límite de intentos: no hay más que anotar."""


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
    tokens: TokenServiceDep,
    platform_tokens: PlatformTokenServiceDep,
    two_factor: TwoFactorStoreDep,
    activity: ActivityRecorderDep,
    response: Response,
    db: SessionDep,
) -> ReauthenticationResponse:
    """Marca esta sesión como reautenticada (RFS-21).

    Datos fiscales, credenciales SOL, certificado, roles y permisos, y desactivar
    cuentas o locales piden la contraseña de nuevo. Vale cinco minutos y solo en
    esta sesión. Los intentos fallidos cuentan como los del acceso: tras varios
    seguidos responde 429.

    Con la verificación en dos pasos activa (ASVS V7.5.1), la contraseña sola no
    alcanza: igual que el acceso, sin `otp_code` responde 401
    `two_factor_required` y con un código equivocado, 401
    `invalid_two_factor_code`, que cuenta como intento fallido. Sirve el código de
    la aplicación o uno de recuperación.

    Con `rotate_session: true` la sesión cambia de identificador (ASVS V7.2.4):
    la respuesta trae `access_token`, `expires_in` y `csrf_token` nuevos, deja
    la cookie nueva y el token de esta petición deja de valer.
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
    factor = await check_second_factor(
        two_factor, db, (kind, account_id), payload.otp_code, on_failure=_nothing
    )
    if factor is SecondFactor.RECOVERY and isinstance(subject, Principal):
        await activity.record(
            subject.restaurant_id, subject.user_id, ActivityKind.RECOVERY_CODE_USED
        )
    await throttle.succeeded(key, address, db)
    moment = await sessions.mark_reauthenticated(db, current)
    logger.info("auth.reauthenticated", account_kind=kind, account_id=account_id)
    confirmed = ReauthenticationResponse(
        reauthenticated_at=moment, valid_until=moment + reauthentication_window()
    )
    if not payload.rotate_session:
        return confirmed
    token, csrf = await rotate_session_token(
        subject,
        current,
        response,
        sessions=sessions,
        db=db,
        tokens=tokens,
        platform_tokens=platform_tokens,
    )
    confirmed.access_token = token.value
    confirmed.token_type = "bearer"  # noqa: S105 - tipo de token de OAuth2, no una contraseña
    confirmed.expires_in = token.expires_in_seconds
    confirmed.csrf_token = csrf
    return confirmed
