"""Identidad compartida por todos los módulos.

Quién es el usuario, qué puede hacer y a qué restaurante pertenece es una
pregunta que se hace cada módulo: el menú, los pedidos y el inventario por
igual. Si la respuesta viviera en `accounts`, todos tendrían que importarlo y
dejaría de haber módulos independientes.

Por eso el núcleo posee la *autenticación* (quién sos) y `accounts` posee la
*gestión de usuarios* (tu perfil, el alta del personal). Este archivo es Python
puro a propósito: lo importa hasta la capa de dominio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from resthub.core.error_codes import ErrorCode


@dataclass(frozen=True, slots=True)
class Principal:
    """Quien hace la petición, reducido a lo que la autorización necesita.

    No es la entidad `User` de `accounts`: no trae nombre ni correo. Un módulo
    que solo tiene que decidir si puede tocar un recurso no necesita conocer el
    perfil de nadie.
    """

    user_id: int
    # Solo para trazas: la autorización mira `permissions`, nunca el rol.
    role_id: int
    is_active: bool
    # El restaurante al que pertenece la cuenta. Todo caso de uso filtra por
    # este valor y nunca por uno que mande el cliente: es la frontera entre
    # restaurantes.
    restaurant_id: int
    # Códigos de permiso de su rol, ya resueltos. Texto y no el enum del
    # catálogo: así este archivo no depende de él.
    permissions: frozenset[str] = field(default_factory=frozenset)
    # La administración del sistema mirando la aplicación como esta cuenta del
    # local de muestra. Los permisos son los del rol, igual que siempre; solo
    # cambia lo que no tiene sentido en una vista previa (renovarla o cambiar
    # la contraseña). `core/auth.py` garantiza que solo vale en un local de
    # muestra.
    preview: bool = False
    # La contraseña la puso otra persona (alta o restablecimiento). Mientras
    # siga así, `core/auth.py` solo deja cambiarla, leer la sesión y salir.
    must_change_password: bool = False
    # La sesión del dispositivo (`core/account_sessions.py`). En una vista previa,
    # la suya (`preview`): solo sirve para cerrarla al salir.
    session_id: str | None = None
    # Bloqueada por inactividad (RFS-29): hasta desbloquearla con el PIN solo
    # se puede leer la sesión, desbloquearla o salir.
    session_locked: bool = False
    # Cuándo se confirmó la contraseña por última vez en esta sesión (RFS-21).
    reauthenticated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_in_seconds: int
    # La sesión del dispositivo (o de la vista previa) que lleva firmada (`sid`).
    session_id: str = ""


# Alcance de una credencial. Una cuenta del personal y una de la administración
# del sistema pueden tener el mismo identificador (son tablas distintas), así
# que el alcance firmado es lo único que impide usar un token en el lado ajeno.
RESTAURANT_SCOPE = "restaurant"
PLATFORM_SCOPE = "platform"

# Vida de un token de vista previa: corta y sin renovación, para que una vista
# previa olvidada en una pestaña muera sola.
PREVIEW_TOKEN_TTL_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: int
    restaurant_id: int
    # Solo en los tokens que emite `POST /auth/preview`.
    preview: bool = False
    # Quién de la administración del sistema abrió la vista previa.
    platform_admin_id: int | None = None
    # Cuándo se emitió (segundos Unix, con fracción). Un token anterior al último corte de
    # sesiones de la cuenta (cambio de contraseña o desactivación) ya no vale.
    issued_at: float = field(default=0.0, compare=False)
    # La sesión del dispositivo que el token representa (`sid`).
    session_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class PlatformClaims:
    admin_id: int
    # Igual que en `TokenClaims`: un token anterior al último corte de sesiones
    # de la cuenta (desactivarla, o cambiarle la contraseña) ya no vale.
    issued_at: float = field(default=0.0, compare=False)
    session_id: str | None = field(default=None, compare=False)


def token_predates(issued_at: float, valid_since: datetime | None) -> bool:
    """Si un token se emitió antes del corte de sesiones de su cuenta.

    `valid_since` es nulo en las cuentas que nunca cortaron sus sesiones.
    """
    if valid_since is None:
        return False
    # SQLite devuelve la fecha sin zona (Postgres, con ella): sin zona es UTC.
    if valid_since.tzinfo is None:
        valid_since = valid_since.replace(tzinfo=UTC)
    return issued_at < valid_since.timestamp()


class TokenService(Protocol):
    """Puerto de emisión y lectura de credenciales portables.

    Qué formato tengan (JWT, PASETO, una fila en Redis) es decisión del
    adaptador. El negocio solo necesita ir de una identidad a un token y volver.
    """

    def issue(
        self, user_id: int, restaurant_id: int, *, session_id: str | None = None
    ) -> AccessToken:
        """`session_id` es la sesión del dispositivo; una nueva si no se indica."""
        ...

    def issue_preview(
        self, user_id: int, restaurant_id: int, platform_admin_id: int
    ) -> AccessToken:
        """Un token de restaurante marcado como vista previa, de vida corta."""
        ...

    def decode(self, token: str) -> TokenClaims:
        """Rechaza con `InvalidTokenError` una credencial de la administración del sistema."""
        ...


class PlatformTokenService(Protocol):
    """Credenciales de la administración del sistema, que no pertenecen a ningún restaurante."""

    def issue_platform(self, admin_id: int, *, session_id: str | None = None) -> AccessToken:
        """`session_id` es la sesión del dispositivo; una nueva si no se indica."""
        ...

    def decode_platform(self, token: str) -> PlatformClaims:
        """Rechaza con `InvalidTokenError` cualquier credencial de restaurante."""
        ...


class IdentityError(Exception):
    """Raíz de los errores de identidad."""

    code: ClassVar[ErrorCode]


class InvalidTokenError(IdentityError):
    code = ErrorCode.INVALID_TOKEN

    def __init__(self, reason: str = "El token no es válido o ya expiró.") -> None:
        super().__init__(reason)
        self.reason = reason


class MissingPermissionError(IdentityError):
    code = ErrorCode.MISSING_PERMISSION

    def __init__(self, required: tuple[str, ...]) -> None:
        super().__init__("Tu rol no tiene permiso para esta acción.")
        self.required = required


def ensure_permission(principal: Principal, *required: str) -> None:
    """Alcanza con tener uno de los permisos pedidos.

    Vive acá y no en el adaptador HTTP para que la misma comprobación sirva a
    un consumidor que no hable HTTP.
    """
    if not any(permission in principal.permissions for permission in required):
        raise MissingPermissionError(tuple(required))
