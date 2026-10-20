"""Autenticación y autorización para el borde HTTP de cualquier módulo.

Es la única pieza del núcleo que lee las tablas de usuarios y roles, y lee solo
lo que la autorización necesita: identificador, estado, restaurante y los
permisos del rol. Esa proyección mínima es el contrato compartido entre
módulos. El resto del perfil y la gestión de los roles los posee `accounts`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import JSON, Boolean, DateTime, text
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.access_history import PLATFORM_ACCOUNT, STAFF_ACCOUNT
from resthub.core.account_sessions import AccountSession, SessionRegistryDep, SqlSessionRegistry
from resthub.core.clock import Clock
from resthub.core.clock_dependency import ClockDep
from resthub.core.config import get_settings
from resthub.core.database import SessionDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.identity import (
    InvalidTokenError,
    MissingPermissionError,
    PlatformClaims,
    PlatformTokenService,
    Principal,
    TokenService,
    ensure_permission,
    token_predates,
)
from resthub.core.permissions import Permission, RoleKind, effective_permissions
from resthub.core.request_context import PREVIEW, STAFF, annotate_account
from resthub.core.session_cookie import PLATFORM_COOKIE, STAFF_COOKIE, token_from_cookie
from resthub.core.tokens import JwtTokenService

# `auto_error=False` para responder con el mensaje del proyecto en vez del
# texto que trae Starlette cuando falta la cabecera.
bearer_scheme = HTTPBearer(auto_error=False, description="Token emitido por /api/v1/auth/login")

UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": "Bearer"}

# Proyección de identidad. Deliberadamente no usa los modelos ORM de `accounts`
# ni de `restaurants`: importarlos convertiría al núcleo en dependiente de
# módulos de dominio. El restaurante entra en la consulta porque desactivarlo
# tiene que cortar el acceso de todo su personal de una vez, y porque un token
# de vista previa solo vale para una cuenta del local de muestra. La cuenta de
# plataforma que abrió la vista previa, porque desactivarla corta también sus
# vistas previas; en un token común el parámetro es nulo y la columna también.
_PRINCIPAL_QUERY = text(
    "SELECT u.id, u.role_id, u.is_active, u.restaurant_id, r.is_active AS restaurant_is_active, "
    "r.is_sandbox AS restaurant_is_sandbox, r.idle_lock_minutes, "
    "u.sessions_valid_since, u.must_change_password, "
    "ro.kind AS role_kind, ro.permissions AS role_permissions, "
    "(SELECT pa.is_active FROM platform_admins pa WHERE pa.id = :platform_admin_id) "
    "AS preview_admin_is_active, "
    "(SELECT pa.sessions_valid_since FROM platform_admins pa WHERE pa.id = :platform_admin_id) "
    "AS preview_admin_sessions_valid_since "
    "FROM users u JOIN restaurants r ON r.id = u.restaurant_id "
    "JOIN roles ro ON ro.id = u.role_id "
    "WHERE u.id = :user_id"
).columns(
    role_permissions=JSON,
    restaurant_is_sandbox=Boolean,
    must_change_password=Boolean,
    preview_admin_is_active=Boolean,
    preview_admin_sessions_valid_since=DateTime(timezone=True),
    sessions_valid_since=DateTime(timezone=True),
)

BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def credentials_from(
    request: Request, bearer: HTTPAuthorizationCredentials | None, cookie: str
) -> HTTPAuthorizationCredentials | None:
    """El token de `Authorization: Bearer` o, si no viene, el de la cookie de ese alcance.

    La cookie exige `X-CSRF-Token` en lo que cambia algo (`core/session_cookie.py`).
    """
    if bearer is not None:
        return bearer
    token = token_from_cookie(request, cookie)
    if token is None:
        return None
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def get_credentials(
    request: Request, bearer: BearerDep
) -> HTTPAuthorizationCredentials | None:
    return credentials_from(request, bearer, STAFF_COOKIE)


CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(get_credentials)]


@dataclass(frozen=True, slots=True)
class _Identity:
    principal: Principal
    # Si la cuenta es del local de muestra. No viaja en el principal: ningún
    # módulo decide nada por eso, solo la lectura del token.
    in_sandbox: bool
    # Si la cuenta de plataforma del token de vista previa sigue activa.
    # `False` sin vista previa: no se preguntó por ninguna.
    preview_admin_active: bool = False
    # Desde cuándo valen las sesiones de esa cuenta de plataforma.
    preview_admin_sessions_valid_since: datetime | None = None
    # Desde cuándo vale la contraseña actual; los tokens anteriores no sirven.
    sessions_valid_since: datetime | None = None
    # Minutos sin uso tras los que se bloquea la sesión (RFS-29); 0, nunca.
    idle_lock_minutes: int = 0


async def _load_identity(
    session: AsyncSession, user_id: int, platform_admin_id: int | None = None
) -> _Identity | None:
    row = (
        await session.execute(
            _PRINCIPAL_QUERY, {"user_id": user_id, "platform_admin_id": platform_admin_id}
        )
    ).one_or_none()
    if row is None:
        return None
    permissions = effective_permissions(RoleKind(row.role_kind), row.role_permissions or ())
    principal = Principal(
        user_id=int(row.id),
        role_id=int(row.role_id),
        # Una cuenta activa de un restaurante desactivado cuenta como inactiva.
        is_active=bool(row.is_active) and bool(row.restaurant_is_active),
        restaurant_id=int(row.restaurant_id),
        permissions=frozenset(permission.value for permission in permissions),
        must_change_password=bool(row.must_change_password),
    )
    return _Identity(
        principal=principal,
        in_sandbox=bool(row.restaurant_is_sandbox),
        preview_admin_active=bool(row.preview_admin_is_active),
        preview_admin_sessions_valid_since=row.preview_admin_sessions_valid_since,
        sessions_valid_since=row.sessions_valid_since,
        idle_lock_minutes=int(row.idle_lock_minutes or 0),
    )


async def load_principal(session: AsyncSession, user_id: int) -> Principal | None:
    """La identidad vigente de una cuenta, o `None` si ya no existe."""
    identity = await _load_identity(session, user_id)
    return identity.principal if identity is not None else None


def get_token_service(clock: ClockDep) -> JwtTokenService:
    # Una sola instancia para los dos alcances: el acceso del personal y el de
    # la administración del sistema firman con la misma clave, y cada lectura
    # rechaza el alcance ajeno.
    settings = get_settings()
    return JwtTokenService(
        secret_key=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        ttl_seconds=settings.access_token_ttl_seconds,
        clock=clock,
    )


TokenServiceDep = Annotated[TokenService, Depends(get_token_service)]
PlatformTokenServiceDep = Annotated[PlatformTokenService, Depends(get_token_service)]


def client_address(request: Request) -> str:
    """La IP de quien pide, para el límite de intentos y el de peticiones."""
    # Con un WAF por delante, la IP real la pone él en su propia cabecera
    # (`CLIENT_IP_HEADER`). Solo es de fiar si el API no se alcanza sin pasar
    # por el WAF; ver el README.
    trusted_header = get_settings().client_ip_header.strip().lower()
    if trusted_header and (address := request.headers.get(trusted_header, "").strip()):
        return address
    # Detrás del proxy de la plataforma, la IP real viene en X-Forwarded-For.
    # Se toma la última: la agrega el proxy. Las de antes las escribe el
    # cliente, y con ellas cualquiera esquivaría el límite cambiándolas.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "desconocida"


def unauthenticated(detail: str, code: ErrorCode = ErrorCode.UNAUTHENTICATED) -> ApiError:
    return ApiError(status.HTTP_401_UNAUTHORIZED, detail, code, UNAUTHENTICATED_HEADERS)


async def ensure_open_session(
    registry: SqlSessionRegistry,
    session: AsyncSession,
    session_id: str | None,
    *,
    account_kind: str,
    account_id: int,
) -> AccountSession:
    """La sesión del dispositivo, si sigue abierta y es de esa cuenta (RFS-17).

    Un token sin `sid` (emitido antes de que existieran las sesiones) ya no vale:
    se vuelve a entrar una vez. Tampoco una sesión abierta hace más que su vida
    máxima, aunque se haya renovado (ASVS V7.3.2). Anota el último uso.
    """
    state = await registry.find(session, session_id) if session_id else None
    if state is None or state.account_kind != account_kind or state.account_id != account_id:
        message = "La sesión se cerró: vuelve a iniciar sesión."
        raise unauthenticated(message)
    # El mismo 401 `unauthenticated` que una sesión cerrada: la interfaz ya lo
    # trata como sesión terminada y lleva al acceso.
    if registry.has_expired(state):
        message = "La sesión venció: vuelve a iniciar sesión."
        raise unauthenticated(message)
    if not registry.is_alive(state):
        message = "La sesión se cerró: vuelve a iniciar sesión."
        raise unauthenticated(message)
    await registry.touch(session, state)
    return state


async def _resolve_principal(
    credentials: HTTPAuthorizationCredentials | None,
    session: AsyncSession,
    tokens: TokenService,
    registry: SqlSessionRegistry,
) -> Principal:
    if credentials is None:
        message = "Falta la credencial de acceso."
        raise unauthenticated(message)

    try:
        claims = tokens.decode(credentials.credentials)
    except InvalidTokenError as error:
        raise unauthenticated(str(error), error.code) from error

    # Permisos, estado y restaurante se releen de la base y no se toman del
    # token: una cuenta desactivada, un mesero que pasó a encargado o un rol al
    # que le quitaron un permiso cambian de acceso en la petición siguiente,
    # sin esperar a que el token expire.
    identity = await _load_identity(session, claims.user_id, claims.platform_admin_id)
    if identity is None or not identity.principal.is_active:
        message = "La cuenta ya no está disponible."
        raise unauthenticated(message)
    principal = identity.principal
    if principal.restaurant_id != claims.restaurant_id:
        message = "La credencial pertenece a otro restaurante."
        raise unauthenticated(message)
    # Cambiar la contraseña o desactivar la cuenta corta las sesiones abiertas:
    # quien se quedó con un token robado no sigue dentro, y un token viejo no
    # revive al reactivar la cuenta. `iat` lleva fracción de segundo, así que
    # lo emitido antes del corte se distingue de lo emitido después.
    if token_predates(claims.issued_at, identity.sessions_valid_since):
        message = "La sesión se cerró: vuelve a iniciar sesión."
        raise unauthenticated(message)
    # Defensa en profundidad: `POST /auth/preview` solo emite tokens de vista
    # previa para cuentas del local de muestra. Si igual apareciera uno para
    # una cuenta real (una clave filtrada, un error aguas arriba), no entra.
    if claims.preview and not identity.in_sandbox:
        message = "La vista previa solo entra al local de muestra."
        raise unauthenticated(message)
    # Una cuenta de plataforma desactivada no sigue mirando por sus vistas
    # previas abiertas: se cortan en la petición siguiente, como las demás.
    # Y reactivarla no las revive: su corte de sesiones también vale aquí.
    if claims.preview and (
        not identity.preview_admin_active
        or token_predates(claims.issued_at, identity.preview_admin_sessions_valid_since)
    ):
        message = "La cuenta de plataforma de esta vista previa ya no está activa."
        raise unauthenticated(message)
    # Para la telemetría del panel de observabilidad: quién hizo la petición.
    annotate_account(
        PREVIEW if claims.preview else STAFF,
        account_id=principal.user_id,
        restaurant_id=principal.restaurant_id,
    )
    if claims.preview:
        return replace(principal, preview=True)
    state = await ensure_open_session(
        registry,
        session,
        claims.session_id,
        account_kind=STAFF_ACCOUNT,
        account_id=principal.user_id,
    )
    locked = state.is_locked or await _lock_if_idle(
        registry, session, state, identity.idle_lock_minutes
    )
    return replace(
        principal,
        session_id=state.id,
        session_locked=locked,
        reauthenticated_at=state.reauthenticated_at,
    )


async def _lock_if_idle(
    registry: SqlSessionRegistry, session: AsyncSession, state: AccountSession, minutes: int
) -> bool:
    """Bloquea la sesión que pasó `minutes` sin usarse (RFS-29).

    La interfaz la bloquea por su cuenta al ver la pantalla quieta; esto cubre
    el celular que se quedó con la aplicación cerrada o en segundo plano: la
    primera petición al volver ya llega bloqueada.
    """
    if minutes <= 0 or registry.clock.now() - state.last_seen_at < timedelta(minutes=minutes):
        return False
    await registry.lock(session, state.id)
    return True


def ensure_password_is_own(principal: Principal) -> Principal:
    """Rechaza a una cuenta que todavía tiene la contraseña temporal (RFS-20).

    Una vista previa no la cambia nunca (la cuenta es del local de muestra y
    quien mira no es su dueño), así que no se le exige.
    """
    if principal.must_change_password and not principal.preview:
        message = "Cambia la contraseña temporal antes de seguir."
        raise ApiError(status.HTTP_403_FORBIDDEN, message, ErrorCode.PASSWORD_CHANGE_REQUIRED)
    return principal


def ensure_unlocked(principal: Principal) -> Principal:
    """Rechaza una sesión bloqueada por inactividad hasta que se desbloquee (RFS-29)."""
    if principal.session_locked:
        message = "La sesión está bloqueada: desbloquéala con tu PIN."
        raise ApiError(status.HTTP_423_LOCKED, message, ErrorCode.SESSION_LOCKED)
    return principal


async def get_principal(
    credentials: CredentialsDep,
    session: SessionDep,
    tokens: TokenServiceDep,
    registry: SessionRegistryDep,
) -> Principal:
    principal = await _resolve_principal(credentials, session, tokens, registry)
    return ensure_unlocked(ensure_password_is_own(principal))


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_principal_with_temporary_password(
    credentials: CredentialsDep,
    session: SessionDep,
    tokens: TokenServiceDep,
    registry: SessionRegistryDep,
) -> Principal:
    """Como `get_principal`, pero deja pasar a quien todavía tiene la contraseña temporal.

    Solo para cambiarla, desbloquear la sesión y cerrarla: el resto de la API
    exige `PrincipalDep`. No rechaza la sesión bloqueada: quien la usa decide.
    """
    return await _resolve_principal(credentials, session, tokens, registry)


TemporaryPasswordPrincipalDep = Annotated[Principal, Depends(get_principal_with_temporary_password)]


async def get_session_subject(
    request: Request,
    bearer: BearerDep,
    session: SessionDep,
    tokens: TokenServiceDep,
    platform_tokens: PlatformTokenServiceDep,
    registry: SessionRegistryDep,
) -> Principal | PlatformClaims:
    """Quien pregunta por su propia sesión: una cuenta del personal o una de plataforma.

    Solo lo usan `GET /auth/me` y `POST /auth/refresh`, que sirven a las dos
    clases de cuenta. Cada token conserva su alcance: uno de plataforma sale
    como `PlatformClaims` (que el módulo de plataforma relee de su tabla), uno
    de restaurante como `Principal`, con todas sus comprobaciones. Ningún otro
    endpoint lo usa: los del restaurante exigen `PrincipalDep`, que rechaza un
    token de plataforma. Sin cabecera, lee la cookie de plataforma o la del
    personal (entrar con una borra la otra).
    """
    credentials = credentials_from(request, bearer, PLATFORM_COOKIE)
    if credentials is None:
        credentials = credentials_from(request, bearer, STAFF_COOKIE)
    if credentials is not None:
        try:
            claims = platform_tokens.decode_platform(credentials.credentials)
        except InvalidTokenError:
            pass  # No es de plataforma: se lee como credencial del personal.
        else:
            await ensure_open_session(
                registry,
                session,
                claims.session_id,
                account_kind=PLATFORM_ACCOUNT,
                account_id=claims.admin_id,
            )
            return claims
    return await _resolve_principal(credentials, session, tokens, registry)


SessionSubjectDep = Annotated[Principal | PlatformClaims, Depends(get_session_subject)]


async def get_active_session_subject(subject: SessionSubjectDep) -> Principal | PlatformClaims:
    """Como `SessionSubjectDep`, pero sin contraseña temporal ni sesión bloqueada.

    Para lo que una cuenta hace sobre su propia seguridad (sus sesiones, su PIN,
    su verificación en dos pasos): nada de eso se toca desde una sesión bloqueada.
    """
    if isinstance(subject, PlatformClaims):
        return subject
    return ensure_unlocked(ensure_password_is_own(subject))


ActiveSessionSubjectDep = Annotated[Principal | PlatformClaims, Depends(get_active_session_subject)]


# Una conexión de avisos queda abierta por horas. La sesión (`SessionDep`, con
# `scope="function"`) se cierra antes de empezar a transmitir, así que no
# retiene una conexión del pool todo ese tiempo.
async def get_stream_principal(
    credentials: CredentialsDep,
    session: SessionDep,
    tokens: TokenServiceDep,
    registry: SessionRegistryDep,
) -> Principal:
    principal = await _resolve_principal(credentials, session, tokens, registry)
    return ensure_unlocked(ensure_password_is_own(principal))


StreamPrincipalDep = Annotated[Principal, Depends(get_stream_principal)]


def require_permission(*required: Permission) -> Callable[[Principal], Awaitable[Principal]]:
    """Exige al menos uno de los permisos pedidos.

    Cada endpoint declara la acción que hace, no quién puede hacerla: quién la
    tiene lo decide cada restaurante al armar sus roles.
    """

    async def dependency(principal: PrincipalDep) -> Principal:
        try:
            ensure_permission(principal, *required)
        except MissingPermissionError as error:
            raise api_error(status.HTTP_403_FORBIDDEN, error) from error
        return principal

    return dependency


def reauthentication_window() -> timedelta:
    """Cuánto vale una contraseña confirmada para las acciones críticas (RFS-21)."""
    return timedelta(seconds=get_settings().reauthentication_window_seconds)


def ensure_recently_authenticated(reauthenticated_at: datetime | None, now: datetime) -> None:
    """Rechaza una acción crítica si la contraseña no se confirmó hace poco en esta sesión.

    Vale solo en el dispositivo donde se confirmó: quien encuentra otra sesión
    abierta de la misma cuenta no la hereda.
    """
    if reauthenticated_at is None or now - reauthenticated_at > reauthentication_window():
        message = "Confirma tu contraseña para seguir."
        raise ApiError(status.HTTP_403_FORBIDDEN, message, ErrorCode.REAUTHENTICATION_REQUIRED)


def require_reauthenticated(
    *required: Permission,
) -> Callable[[Principal, Clock], Awaitable[Principal]]:
    """Como `require_permission`, y además la contraseña confirmada hace poco (RFS-21)."""
    allowed = require_permission(*required)

    async def dependency(principal: PrincipalDep, clock: ClockDep) -> Principal:
        await allowed(principal)
        ensure_recently_authenticated(principal.reauthenticated_at, clock.now())
        return principal

    return dependency
