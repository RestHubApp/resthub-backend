from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

import jwt

from resthub.core.clock import Clock, SystemClock
from resthub.core.identity import (
    PLATFORM_SCOPE,
    PREVIEW_TOKEN_TTL_SECONDS,
    RESTAURANT_SCOPE,
    AccessToken,
    InvalidTokenError,
    PlatformClaims,
    TokenClaims,
)

DEFAULT_ISSUER = "resthub"
DEFAULT_AUDIENCE = "resthub-api"
DEFAULT_PLATFORM_AUDIENCE = "resthub-platform"
# Lo que todo token de este servidor trae; uno sin alguno de ellos no lo emitió él.
REQUIRED_CLAIMS = ("exp", "iat", "sub", "iss", "aud")


def _session_id(payload: dict[str, object]) -> str | None:
    """La sesión del dispositivo; `None` en una vista previa o en un token de antes."""
    session_id = payload.get("sid")
    return session_id if isinstance(session_id, str) and session_id else None


def _issued_at(payload: dict[str, object]) -> float:
    """Cuándo se emitió, en segundos Unix; cero si el token no lo trae."""
    issued_at = payload.get("iat")
    if isinstance(issued_at, int | float) and not isinstance(issued_at, bool):
        return float(issued_at)
    return 0.0


@dataclass(frozen=True, slots=True)
class TokenAudiences:
    """Quién emite (`iss`) y para quién es cada clase de token (`aud`), ASVS V9.2.3."""

    issuer: str = DEFAULT_ISSUER
    restaurant: str = DEFAULT_AUDIENCE
    platform: str = DEFAULT_PLATFORM_AUDIENCE


class JwtTokenService:
    """Emite y lee los dos tipos de credencial: la del personal y la de la plataforma.

    Comparten clave y formato, así que la frontera entre ambas es doble: el
    `scope` firmado y la audiencia (`aud`); cada lectura exige los suyos y
    rechaza los del otro. Toda lectura exige además el emisor (`iss`), `exp`,
    `iat` y `sub`, y solo el algoritmo configurado (nunca `none`).
    """

    def __init__(
        self,
        secret_key: str,
        algorithm: str,
        ttl_seconds: int,
        preview_ttl_seconds: int = PREVIEW_TOKEN_TTL_SECONDS,
        clock: Clock | None = None,
        audiences: TokenAudiences | None = None,
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._secret_key = secret_key
        self._algorithm = algorithm
        self._ttl_seconds = ttl_seconds
        self._preview_ttl_seconds = preview_ttl_seconds
        self._audiences = audiences if audiences is not None else TokenAudiences()

    def issue(
        self, user_id: int, restaurant_id: int, *, session_id: str | None = None
    ) -> AccessToken:
        # Sin rol: lo que la cuenta puede hacer se relee de la base en cada
        # petición, así que llevarlo en el token solo lo dejaría desactualizado.
        return self._sign(
            {
                "sub": str(user_id),
                "scope": RESTAURANT_SCOPE,
                # Viaja firmado para que el servidor lo compare contra la base: un
                # token emitido para un restaurante no sirve si la cuenta ya no está ahí.
                "restaurant_id": restaurant_id,
                "sid": session_id or secrets.token_urlsafe(32),
            }
        )

    def issue_preview(
        self, user_id: int, restaurant_id: int, platform_admin_id: int
    ) -> AccessToken:
        # Un token de restaurante como cualquiera, así que abre la aplicación
        # igual que el de la cuenta; las dos marcas solo restringen. Quién abrió
        # la vista previa viaja firmado para las trazas.
        return self._sign(
            {
                "sub": str(user_id),
                "scope": RESTAURANT_SCOPE,
                "restaurant_id": restaurant_id,
                "preview": True,
                "platform_admin_id": platform_admin_id,
            },
            ttl_seconds=self._preview_ttl_seconds,
        )

    def issue_platform(self, admin_id: int, *, session_id: str | None = None) -> AccessToken:
        return self._sign(
            {
                "sub": str(admin_id),
                "scope": PLATFORM_SCOPE,
                "sid": session_id or secrets.token_urlsafe(32),
            },
            audience=self._audiences.platform,
        )

    def decode(self, token: str) -> TokenClaims:
        payload = self._verify(token, self._audiences.restaurant)
        # Los tokens emitidos antes de la administración del sistema no traen
        # alcance, y todos eran de restaurante.
        if payload.get("scope", RESTAURANT_SCOPE) != RESTAURANT_SCOPE:
            message = "La credencial no es de una cuenta del restaurante."
            raise InvalidTokenError(message)
        return self._to_claims(payload)

    def decode_platform(self, token: str) -> PlatformClaims:
        payload = self._verify(token, self._audiences.platform)
        if payload.get("scope") != PLATFORM_SCOPE:
            message = "La credencial no es de la administración del sistema."
            raise InvalidTokenError(message)
        return PlatformClaims(
            admin_id=self._subject(payload),
            issued_at=_issued_at(payload),
            session_id=_session_id(payload),
        )

    def _sign(
        self,
        claims: dict[str, object],
        ttl_seconds: int | None = None,
        audience: str | None = None,
    ) -> AccessToken:
        ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        issued_at = self._clock.now()
        payload = {
            **claims,
            "iss": self._audiences.issuer,
            "aud": audience if audience is not None else self._audiences.restaurant,
            # Con fracción de segundo: el corte de sesiones (cambiar la
            # contraseña, desactivar) distingue lo emitido antes y después en
            # el mismo segundo.
            "iat": issued_at.timestamp(),
            "exp": (issued_at + timedelta(seconds=ttl)).timestamp(),
        }
        token = jwt.encode(payload, self._secret_key, algorithm=self._algorithm)
        session_id = claims.get("sid")
        return AccessToken(
            value=token,
            expires_in_seconds=ttl,
            session_id=session_id if isinstance(session_id, str) else "",
        )

    def _verify(self, token: str, audience: str) -> dict[str, object]:
        try:
            return jwt.decode(
                token,
                self._secret_key,
                # Solo el algoritmo configurado: un token con `alg: none` o con otro
                # algoritmo no se lee.
                algorithms=[self._algorithm],
                audience=audience,
                issuer=self._audiences.issuer,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError as error:
            raise InvalidTokenError from error

    @staticmethod
    def _subject(payload: dict[str, object]) -> int:
        raw_subject = payload.get("sub")
        if not isinstance(raw_subject, str):
            message = "El token no trae sujeto."
            raise InvalidTokenError(message)
        try:
            return int(raw_subject)
        except ValueError as error:
            message = "El token trae un sujeto desconocido."
            raise InvalidTokenError(message) from error

    @classmethod
    def _to_claims(cls, payload: dict[str, object]) -> TokenClaims:
        # Un token emitido antes de los roles por restaurante trae además un
        # `role`; se ignora y sigue sirviendo hasta vencer.
        raw_restaurant = payload.get("restaurant_id")
        # `bool` es subclase de `int`: sin excluirlo, un `true` pasaría por restaurante.
        if (
            not isinstance(payload.get("sub"), str)
            or not isinstance(raw_restaurant, int)
            or isinstance(raw_restaurant, bool)
        ):
            message = "El token no trae sujeto ni restaurante."
            raise InvalidTokenError(message)
        preview = payload.get("preview", False)
        # Estricto: un valor que no sea exactamente `true` o `false` es un token
        # que este servidor no emitió.
        if not isinstance(preview, bool):
            message = "El token trae una marca de vista previa desconocida."
            raise InvalidTokenError(message)
        admin_id = payload.get("platform_admin_id")
        platform_admin_id: int | None = None
        if preview:
            if not isinstance(admin_id, int) or isinstance(admin_id, bool):
                message = "El token de vista previa no dice quién la abrió."
                raise InvalidTokenError(message)
            platform_admin_id = admin_id
        return TokenClaims(
            user_id=cls._subject(payload),
            restaurant_id=raw_restaurant,
            preview=preview,
            platform_admin_id=platform_admin_id,
            issued_at=_issued_at(payload),
            session_id=None if preview else _session_id(payload),
        )
