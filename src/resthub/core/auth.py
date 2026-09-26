"""Autenticación y autorización para el borde HTTP de cualquier módulo.

Es la única pieza del núcleo que lee las tablas de usuarios y roles, y lee solo
lo que la autorización necesita: identificador, estado, restaurante y los
permisos del rol. Esa proyección mínima es el contrato compartido entre
módulos. El resto del perfil y la gestión de los roles los posee `accounts`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import JSON, Boolean, text
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.config import get_settings
from resthub.core.database import get_session
from resthub.core.identity import (
    InvalidToken,
    MissingPermission,
    PlatformTokenService,
    Principal,
    TokenService,
    ensure_permission,
)
from resthub.core.permissions import Permission, RoleKind, effective_permissions
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
    "r.is_sandbox AS restaurant_is_sandbox, "
    "ro.kind AS role_kind, ro.permissions AS role_permissions, "
    "(SELECT pa.is_active FROM platform_admins pa WHERE pa.id = :platform_admin_id) "
    "AS preview_admin_is_active "
    "FROM users u JOIN restaurants r ON r.id = u.restaurant_id "
    "JOIN roles ro ON ro.id = u.role_id "
    "WHERE u.id = :user_id"
).columns(role_permissions=JSON, restaurant_is_sandbox=Boolean, preview_admin_is_active=Boolean)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


@dataclass(frozen=True, slots=True)
class _Identity:
    principal: Principal
    # Si la cuenta es del local de muestra. No viaja en el principal: ningún
    # módulo decide nada por eso, solo la lectura del token.
    in_sandbox: bool
    # Si la cuenta de plataforma del token de vista previa sigue activa.
    # `False` sin vista previa: no se preguntó por ninguna.
    preview_admin_active: bool = False


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
    )
    return _Identity(
        principal=principal,
        in_sandbox=bool(row.restaurant_is_sandbox),
        preview_admin_active=bool(row.preview_admin_is_active),
    )


async def load_principal(session: AsyncSession, user_id: int) -> Principal | None:
    """La identidad vigente de una cuenta, o `None` si ya no existe."""
    identity = await _load_identity(session, user_id)
    return identity.principal if identity is not None else None


def get_token_service() -> JwtTokenService:
    # Una sola instancia para los dos alcances: el acceso del personal y el de
    # la administración del sistema firman con la misma clave, y cada lectura
    # rechaza el alcance ajeno.
    settings = get_settings()
    return JwtTokenService(
        secret_key=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        ttl_seconds=settings.access_token_ttl_seconds,
    )


TokenServiceDep = Annotated[TokenService, Depends(get_token_service)]
PlatformTokenServiceDep = Annotated[PlatformTokenService, Depends(get_token_service)]


def client_address(request: Request) -> str:
    """La IP de quien intenta entrar, para el límite de intentos."""
    # Detrás del proxy de la plataforma, la IP real viene en X-Forwarded-For.
    # Se toma la última: la agrega el proxy. Las de antes las escribe el
    # cliente, y con ellas cualquiera esquivaría el límite cambiándolas.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "desconocida"


def unauthenticated(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers=UNAUTHENTICATED_HEADERS)


async def _resolve_principal(
    credentials: HTTPAuthorizationCredentials | None,
    session: AsyncSession,
    tokens: TokenService,
) -> Principal:
    if credentials is None:
        raise unauthenticated("Falta la credencial de acceso.")

    try:
        claims = tokens.decode(credentials.credentials)
    except InvalidToken as error:
        raise unauthenticated(str(error)) from error

    # Permisos, estado y restaurante se releen de la base y no se toman del
    # token: una cuenta desactivada, un mesero que pasó a encargado o un rol al
    # que le quitaron un permiso cambian de acceso en la petición siguiente,
    # sin esperar a que el token expire.
    identity = await _load_identity(session, claims.user_id, claims.platform_admin_id)
    if identity is None or not identity.principal.is_active:
        raise unauthenticated("La cuenta ya no está disponible.")
    principal = identity.principal
    if principal.restaurant_id != claims.restaurant_id:
        raise unauthenticated("La credencial pertenece a otro restaurante.")
    # Defensa en profundidad: `POST /auth/preview` solo emite tokens de vista
    # previa para cuentas del local de muestra. Si igual apareciera uno para
    # una cuenta real (una clave filtrada, un error aguas arriba), no entra.
    if claims.preview and not identity.in_sandbox:
        raise unauthenticated("La vista previa solo entra al local de muestra.")
    # Una cuenta de plataforma desactivada no sigue mirando por sus vistas
    # previas abiertas: se cortan en la petición siguiente, como las demás.
    if claims.preview and not identity.preview_admin_active:
        raise unauthenticated("La cuenta de plataforma de esta vista previa ya no está activa.")
    return replace(principal, preview=claims.preview)


async def get_principal(
    credentials: CredentialsDep,
    session: SessionDep,
    tokens: TokenServiceDep,
) -> Principal:
    return await _resolve_principal(credentials, session, tokens)


PrincipalDep = Annotated[Principal, Depends(get_principal)]

# Una conexión de avisos queda abierta por horas. Con la sesión de siempre, que
# se cierra al terminar la respuesta, retendría una conexión del pool todo ese
# tiempo; con `scope="function"` la sesión se cierra antes de empezar a
# transmitir.
_ShortSessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


async def get_stream_principal(
    credentials: CredentialsDep,
    session: _ShortSessionDep,
    tokens: TokenServiceDep,
) -> Principal:
    return await _resolve_principal(credentials, session, tokens)


StreamPrincipalDep = Annotated[Principal, Depends(get_stream_principal)]


def require_permission(*required: Permission) -> Callable[[Principal], Awaitable[Principal]]:
    """Exige al menos uno de los permisos pedidos.

    Cada endpoint declara la acción que hace, no quién puede hacerla: quién la
    tiene lo decide cada restaurante al armar sus roles.
    """

    async def dependency(principal: PrincipalDep) -> Principal:
        try:
            ensure_permission(principal, *required)
        except MissingPermission as error:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
        return principal

    return dependency
