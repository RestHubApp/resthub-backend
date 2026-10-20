"""Adaptador HTTP del bloqueo por inactividad con PIN (RFS-29).

Solo para cuentas del personal: la plataforma no se usa en un celular compartido.
La interfaz bloquea la sesión al ver la pantalla quieta los minutos que fija el
local (`POST /auth/me/lock`); `core/auth.py` además bloquea la que vuelve después
de ese tiempo sin uso. Mientras tanto, el resto de la API responde 423
`session_locked`. Se desbloquea con el PIN o, sin PIN, con la contraseña; al
tercer intento fallido la sesión se cierra.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field, model_validator

from resthub.core.account_sessions import SessionRegistryDep
from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import (
    PrincipalDep,
    TemporaryPasswordPrincipalDep,
    TokenServiceDep,
    ensure_recently_authenticated,
)
from resthub.core.clock_dependency import ClockDep
from resthub.core.database import SessionDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.identity import Principal
from resthub.core.logs import get_logger
from resthub.core.session_cookie import STAFF_COOKIE, attach_session
from resthub.modules.accounts.adapters.api.dependencies import (
    PasswordHasherDep,
    RestaurantDirectoryDep,
    UserRepositoryDep,
)
from resthub.modules.accounts.adapters.api.schemas import StaffAccessTokenResponse
from resthub.modules.accounts.domain.entities import PIN_LENGTHS
from resthub.modules.accounts.domain.exceptions import (
    InvalidPinError,
    PinNotSetError,
    PreviewSessionRestrictedError,
    UserNotFoundError,
    WrongPinError,
)
from resthub.modules.accounts.use_cases.read_session import ReadCurrentSession
from resthub.modules.accounts.use_cases.unlock_pin import (
    SetOwnPin,
    SetOwnPinCommand,
    VerifyUnlockSecret,
    VerifyUnlockSecretCommand,
)

router = APIRouter()
logger = get_logger("resthub.auth.idle_lock")

# Al tercer PIN equivocado la sesión se cierra (RFS-29).
MAX_UNLOCK_FAILURES = 3
PinField = Annotated[str, Field(min_length=min(PIN_LENGTHS), max_length=max(PIN_LENGTHS))]


class SetPinRequest(BaseModel):
    pin: PinField


class UnlockRequest(BaseModel):
    """El PIN o, si la cuenta no tiene (o lo olvidó), la contraseña: uno de los dos."""

    pin: PinField | None = None
    password: str | None = Field(default=None, max_length=128)
    # Con `true`, al desbloquear la sesión cambia de identificador (ASVS V7.2.4):
    # responde 200 con el token nuevo, con la forma del acceso, y el de esta
    # petición deja de valer. Sin él, 204 como siempre.
    rotate_session: bool = False

    @model_validator(mode="after")
    def _exactly_one(self) -> UnlockRequest:
        if (self.pin is None) == (self.password is None):
            message = "Manda el PIN o la contraseña."
            raise ValueError(message)
        return self


def _own_session(principal: Principal) -> str:
    if principal.preview or principal.session_id is None:
        raise api_error(status.HTTP_403_FORBIDDEN, PreviewSessionRestrictedError())
    return principal.session_id


async def _set_pin(
    principal: Principal,
    pin: str | None,
    *,
    users: UserRepositoryDep,
    hasher: PasswordHasherDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> None:
    """Elegir o quitar el PIN pide la contraseña confirmada hace poco, como RFS-21."""
    _own_session(principal)
    ensure_recently_authenticated(principal.reauthenticated_at, clock.now())
    try:
        await SetOwnPin(users, hasher, activity)(
            SetOwnPinCommand(user_id=principal.user_id, pin=pin, preview=principal.preview)
        )
    except InvalidPinError as error:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error) from error
    except UserNotFoundError as error:
        raise api_error(status.HTTP_404_NOT_FOUND, error) from error


@router.put(
    "/me/pin", status_code=status.HTTP_204_NO_CONTENT, summary="Elegir el PIN de desbloqueo"
)
async def set_own_pin(
    payload: SetPinRequest,
    principal: PrincipalDep,
    users: UserRepositoryDep,
    hasher: PasswordHasherDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> None:
    """De 4 a 6 dígitos. Reemplaza el anterior si había uno."""
    await _set_pin(
        principal, payload.pin, users=users, hasher=hasher, activity=activity, clock=clock
    )


@router.delete(
    "/me/pin", status_code=status.HTTP_204_NO_CONTENT, summary="Quitar el PIN de desbloqueo"
)
async def remove_own_pin(
    principal: PrincipalDep,
    users: UserRepositoryDep,
    hasher: PasswordHasherDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> None:
    """Sin PIN, la sesión bloqueada se retoma con la contraseña."""
    await _set_pin(principal, None, users=users, hasher=hasher, activity=activity, clock=clock)


@router.post(
    "/me/lock", status_code=status.HTTP_204_NO_CONTENT, summary="Bloquear la sesión de este equipo"
)
async def lock_own_session(
    principal: TemporaryPasswordPrincipalDep, sessions: SessionRegistryDep, db: SessionDep
) -> None:
    """Lo llama la interfaz al cumplirse los minutos sin uso, o a pedido."""
    session_id = _own_session(principal)
    if not principal.session_locked:
        await sessions.lock(db, session_id)


@router.post(
    "/me/unlock",
    status_code=status.HTTP_204_NO_CONTENT,
    # El 200 con token solo con `rotate_session`: lo documenta `responses`.
    response_model=None,
    summary="Desbloquear la sesión con el PIN o la contraseña",
    responses={
        status.HTTP_200_OK: {
            "model": StaffAccessTokenResponse,
            "description": "Desbloqueada con `rotate_session`: el token nuevo y la sesión.",
        }
    },
)
async def unlock_own_session(
    payload: UnlockRequest,
    principal: TemporaryPasswordPrincipalDep,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    hasher: PasswordHasherDep,
    sessions: SessionRegistryDep,
    tokens: TokenServiceDep,
    activity: ActivityRecorderDep,
    response: Response,
    db: SessionDep,
) -> StaffAccessTokenResponse | None:
    """Un PIN equivocado responde 400 con los intentos que quedan; el tercero cierra la sesión.

    Con `rotate_session: true`, desbloquear responde 200 con un token nuevo (otro
    `sid`, ASVS V7.2.4) y la sesión, como `POST /auth/refresh`; el token de esta
    petición deja de valer. Una sesión que no estaba bloqueada responde 204 y
    conserva su token.
    """
    session_id = _own_session(principal)
    if not principal.session_locked:
        return None
    try:
        matches = await VerifyUnlockSecret(users, hasher)(
            VerifyUnlockSecretCommand(
                user_id=principal.user_id, pin=payload.pin, password=payload.password
            )
        )
    except PinNotSetError as error:
        raise api_error(status.HTTP_409_CONFLICT, error) from error
    except UserNotFoundError as error:
        raise api_error(status.HTTP_404_NOT_FOUND, error) from error
    if matches:
        await sessions.unlock(db, session_id)
        if not payload.rotate_session:
            return None
        return await _rotated_session(
            principal,
            response,
            users=users,
            restaurants=restaurants,
            sessions=sessions,
            tokens=tokens,
            db=db,
        )
    state = await sessions.find(db, session_id)
    failures = await sessions.count_unlock_failure(db, state) if state else MAX_UNLOCK_FAILURES
    # Cada intento fallido queda en el log, no solo el que cierra la sesión.
    logger.warning(
        "auth.unlock_failed",
        user_id=principal.user_id,
        method="pin" if payload.pin is not None else "password",
        failures=failures,
        attempts_left=max(MAX_UNLOCK_FAILURES - failures, 0),
    )
    if failures >= MAX_UNLOCK_FAILURES:
        await sessions.revoke(db, session_id)
        await activity.record(
            principal.restaurant_id, principal.user_id, ActivityKind.SESSION_LOCKED_OUT
        )
        logger.warning("auth.session_locked_out", user_id=principal.user_id)
    # Se confirma antes de responder el error: una petición con error se deshace,
    # y el intento no contaría (o la sesión quedaría abierta para un cuarto).
    await db.commit()
    if failures < MAX_UNLOCK_FAILURES:
        raise api_error(status.HTTP_400_BAD_REQUEST, WrongPinError(MAX_UNLOCK_FAILURES - failures))
    message = "Se cerró la sesión tras tres intentos fallidos: vuelve a iniciar sesión."
    raise ApiError(status.HTTP_401_UNAUTHORIZED, message, ErrorCode.UNLOCK_ATTEMPTS_EXHAUSTED)


async def _rotated_session(
    principal: Principal,
    response: Response,
    *,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    sessions: SessionRegistryDep,
    tokens: TokenServiceDep,
    db: SessionDep,
) -> StaffAccessTokenResponse:
    """La sesión recién desbloqueada con otro `sid`: token nuevo, cookie nueva y 200."""
    state = await sessions.find(db, principal.session_id or "")
    if state is None:
        message = "La sesión se cerró: vuelve a iniciar sesión."
        raise ApiError(status.HTTP_401_UNAUTHORIZED, message, ErrorCode.UNAUTHENTICATED)
    try:
        current = await ReadCurrentSession(users, restaurants)(principal.user_id)
    except UserNotFoundError as error:
        raise api_error(status.HTTP_404_NOT_FOUND, error) from error
    new_id = await sessions.rotate(db, state)
    token = tokens.issue(principal.user_id, principal.restaurant_id, session_id=new_id)
    issued = StaffAccessTokenResponse.issued(current, token.value, token.expires_in_seconds)
    issued.csrf_token = attach_session(
        response, STAFF_COOKIE, token.value, token.expires_in_seconds
    )
    response.status_code = status.HTTP_200_OK
    logger.info("auth.session_rotated", user_id=principal.user_id)
    return issued
