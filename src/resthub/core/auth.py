"""Autenticación y autorización para el borde HTTP de cualquier módulo.

Es la única pieza del núcleo que lee las tablas de usuarios y roles, y lee solo
lo que la autorización necesita: identificador, estado, restaurante y los
permisos del rol. Esa proyección mínima es el contrato compartido entre
módulos. El resto del perfil y la gestión de los roles los posee `accounts`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import JSON, text
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.config import get_settings
from resthub.core.database import get_session
from resthub.core.identity import (
    InvalidToken,
    MissingPermission,
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
# tiene que cortar el acceso de todo su personal de una vez.
_PRINCIPAL_QUERY = text(
    "SELECT u.id, u.role_id, u.is_active, u.restaurant_id, r.is_active AS restaurant_is_active, "
    "ro.kind AS role_kind, ro.permissions AS role_permissions "
    "FROM users u JOIN restaurants r ON r.id = u.restaurant_id "
    "JOIN roles ro ON ro.id = u.role_id "
    "WHERE u.id = :user_id"
).columns(role_permissions=JSON)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


async def load_principal(session: AsyncSession, user_id: int) -> Principal | None:
    """La identidad vigente de una cuenta, o `None` si ya no existe."""
    row = (await session.execute(_PRINCIPAL_QUERY, {"user_id": user_id})).one_or_none()
    if row is None:
        return None
    permissions = effective_permissions(RoleKind(row.role_kind), row.role_permissions or ())
    return Principal(
        user_id=int(row.id),
        role_id=int(row.role_id),
        # Una cuenta activa de un restaurante desactivado cuenta como inactiva.
        is_active=bool(row.is_active) and bool(row.restaurant_is_active),
        restaurant_id=int(row.restaurant_id),
        permissions=frozenset(permission.value for permission in permissions),
    )


def get_token_service() -> TokenService:
    settings = get_settings()
    return JwtTokenService(
        secret_key=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        ttl_seconds=settings.access_token_ttl_seconds,
    )


TokenServiceDep = Annotated[TokenService, Depends(get_token_service)]


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
    principal = await load_principal(session, claims.user_id)
    if principal is None or not principal.is_active:
        raise unauthenticated("La cuenta ya no está disponible.")
    if principal.restaurant_id != claims.restaurant_id:
        raise unauthenticated("La credencial pertenece a otro restaurante.")
    return principal


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
