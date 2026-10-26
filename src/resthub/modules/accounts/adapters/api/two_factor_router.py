"""Adaptador HTTP de la verificación en dos pasos de la cuenta propia (RFS-22).

Sirve a las dos clases de cuenta. Activarla, renovar los códigos de
recuperación o apagarla pide la contraseña confirmada hace poco en esta sesión
(RFS-21): quien encuentra una sesión abierta no puede ponerle su propio celular.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from resthub.core.account_sessions import SessionRegistryDep
from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import ActiveSessionSubjectDep
from resthub.core.clock_dependency import ClockDep
from resthub.core.database import SessionDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError
from resthub.core.identity import PlatformClaims, Principal
from resthub.core.logs import get_logger
from resthub.core.two_factor import PlatformTwoFactorPolicyDep, TwoFactorStoreDep, otpauth_uri
from resthub.modules.accounts.adapters.api.dependencies import (
    PlatformSignInDep,
    UserRepositoryDep,
)
from resthub.modules.accounts.adapters.api.security_router import (
    account_owner,
    ensure_subject_reauthenticated,
)

router = APIRouter()
logger = get_logger("resthub.auth.two_factor")


class TwoFactorStatusResponse(BaseModel):
    enabled: bool
    # El alta empezó y espera el primer código.
    pending: bool
    recovery_codes_left: int
    # Quedan códigos cortos de antes (10 caracteres): la interfaz invita a renovarlos.
    recovery_codes_outdated: bool = False


class TwoFactorSetupResponse(BaseModel):
    """Lo que la aplicación de códigos necesita: el QR codifica `otpauth_uri`."""

    secret: str
    otpauth_uri: str


class TwoFactorCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class RecoveryCodesResponse(BaseModel):
    """Se muestran una sola vez: cada uno sirve para entrar sin el celular.

    Cada código viene ya agrupado (`K7QM-4XWP-9RTA-...`, 34 caracteres): se
    muestra tal cual. Al entrar se aceptan con o sin guiones y espacios, en
    mayúsculas o minúsculas.
    """

    recovery_codes: list[str]


def _conflict(message: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, message, ErrorCode.TWO_FACTOR_STATE_CONFLICT)


async def _record(
    activity: ActivityRecorderDep, subject: Principal | PlatformClaims, kind: ActivityKind
) -> None:
    # La bitácora es la del local; la plataforma lo deja en los logs.
    if isinstance(subject, Principal):
        await activity.record(subject.restaurant_id, subject.user_id, kind)


@router.get(
    "/me/two-factor",
    response_model=TwoFactorStatusResponse,
    summary="Estado de la verificación en dos pasos",
)
async def read_two_factor(
    subject: ActiveSessionSubjectDep, store: TwoFactorStoreDep, db: SessionDep
) -> TwoFactorStatusResponse:
    kind, account_id, _ = account_owner(subject)
    found = await store.status(db, kind, account_id)
    return TwoFactorStatusResponse(
        enabled=found.enabled,
        pending=found.pending,
        recovery_codes_left=found.recovery_codes_left,
        recovery_codes_outdated=found.recovery_codes_outdated,
    )


@router.post(
    "/me/two-factor/setup",
    response_model=TwoFactorSetupResponse,
    summary="Empezar el alta de la verificación en dos pasos",
)
async def begin_two_factor(
    subject: ActiveSessionSubjectDep,
    store: TwoFactorStoreDep,
    sessions: SessionRegistryDep,
    users: UserRepositoryDep,
    platform: PlatformSignInDep,
    clock: ClockDep,
    db: SessionDep,
) -> TwoFactorSetupResponse:
    """Un secreto nuevo, pendiente hasta confirmarlo con un código de la aplicación."""
    await ensure_subject_reauthenticated(subject, sessions, db, clock)
    kind, account_id, _ = account_owner(subject)
    if await store.is_enabled(db, kind, account_id):
        message = (
            "La verificación en dos pasos ya está activa: desactívala para cambiar el celular."
        )
        raise _conflict(message)
    if isinstance(subject, Principal):
        user = await users.get(subject.user_id)
        label = user.email if user else str(account_id)
    else:
        account = await platform.read(subject.admin_id, subject.issued_at)
        label = account.email if account else str(account_id)
    secret = await store.begin(db, kind, account_id)
    return TwoFactorSetupResponse(secret=secret, otpauth_uri=otpauth_uri(secret, label))


@router.post(
    "/me/two-factor/confirm",
    response_model=RecoveryCodesResponse,
    summary="Activar la verificación en dos pasos con el primer código",
)
async def confirm_two_factor(
    payload: TwoFactorCodeRequest,
    subject: ActiveSessionSubjectDep,
    store: TwoFactorStoreDep,
    sessions: SessionRegistryDep,
    activity: ActivityRecorderDep,
    db: SessionDep,
) -> RecoveryCodesResponse:
    """Responde los diez códigos de recuperación: no se vuelven a mostrar.

    Cierra las demás sesiones de la cuenta (ASVS V7.4.3): las abiertas solo con
    la contraseña tienen que volver a entrar con el segundo paso.
    """
    kind, account_id, current = account_owner(subject)
    if not (await store.status(db, kind, account_id)).pending:
        message = "No hay un alta de la verificación en dos pasos esperando el código."
        raise _conflict(message)
    codes = await store.confirm(db, kind, account_id, payload.code)
    if codes is None:
        message = "El código no es correcto: revisa la hora del celular y vuelve a intentar."
        raise ApiError(status.HTTP_400_BAD_REQUEST, message, ErrorCode.INVALID_TWO_FACTOR_CODE)
    await sessions.revoke_all(db, kind, account_id, keep=current)
    await _record(activity, subject, ActivityKind.TWO_FACTOR_ENABLED)
    logger.info("auth.two_factor_enabled", account_kind=kind, account_id=account_id)
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post(
    "/me/two-factor/recovery-codes",
    response_model=RecoveryCodesResponse,
    summary="Cambiar los códigos de recuperación",
)
async def renew_recovery_codes(
    subject: ActiveSessionSubjectDep,
    store: TwoFactorStoreDep,
    sessions: SessionRegistryDep,
    clock: ClockDep,
    db: SessionDep,
) -> RecoveryCodesResponse:
    """Los anteriores dejan de servir."""
    await ensure_subject_reauthenticated(subject, sessions, db, clock)
    kind, account_id, _ = account_owner(subject)
    codes = await store.renew_recovery_codes(db, kind, account_id)
    if codes is None:
        message = "La verificación en dos pasos no está activa."
        raise _conflict(message)
    return RecoveryCodesResponse(recovery_codes=codes)


@router.delete(
    "/me/two-factor",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desactivar la verificación en dos pasos",
)
async def disable_two_factor(
    subject: ActiveSessionSubjectDep,
    store: TwoFactorStoreDep,
    sessions: SessionRegistryDep,
    activity: ActivityRecorderDep,
    platform_required: PlatformTwoFactorPolicyDep,
    clock: ClockDep,
    db: SessionDep,
) -> None:
    """La administración del sistema no puede quedarse sin ella: la cambia renovándola.

    Cierra las demás sesiones de la cuenta (ASVS V7.4.3), como al activarla.
    """
    if isinstance(subject, PlatformClaims) and platform_required:
        message = "La administración del sistema no puede usarse sin la verificación en dos pasos."
        raise _conflict(message)
    await ensure_subject_reauthenticated(subject, sessions, db, clock)
    kind, account_id, current = account_owner(subject)
    if await store.disable(db, kind, account_id):
        await sessions.revoke_all(db, kind, account_id, keep=current)
        await _record(activity, subject, ActivityKind.TWO_FACTOR_DISABLED)
        logger.info("auth.two_factor_disabled", account_kind=kind, account_id=account_id)
