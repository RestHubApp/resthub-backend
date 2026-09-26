from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from resthub.core.identity import (
    PLATFORM_SCOPE,
    PREVIEW_TOKEN_TTL_SECONDS,
    RESTAURANT_SCOPE,
    AccessToken,
    InvalidToken,
    PlatformClaims,
    TokenClaims,
)


class JwtTokenService:
    """Emite y lee los dos tipos de credencial: la del personal y la de la plataforma.

    Comparten clave y formato, así que la frontera entre ambas es el `scope`
    firmado: cada lectura exige el suyo y rechaza el otro.
    """

    def __init__(
        self,
        secret_key: str,
        algorithm: str,
        ttl_seconds: int,
        preview_ttl_seconds: int = PREVIEW_TOKEN_TTL_SECONDS,
    ) -> None:
        self._secret_key = secret_key
        self._algorithm = algorithm
        self._ttl_seconds = ttl_seconds
        self._preview_ttl_seconds = preview_ttl_seconds

    def issue(self, user_id: int, restaurant_id: int) -> AccessToken:
        # Sin rol: lo que la cuenta puede hacer se relee de la base en cada
        # petición, así que llevarlo en el token solo lo dejaría desactualizado.
        return self._sign(
            {
                "sub": str(user_id),
                "scope": RESTAURANT_SCOPE,
                # Viaja firmado para que el servidor lo compare contra la base: un
                # token emitido para un restaurante no sirve si la cuenta ya no está ahí.
                "restaurant_id": restaurant_id,
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

    def issue_platform(self, admin_id: int) -> AccessToken:
        return self._sign({"sub": str(admin_id), "scope": PLATFORM_SCOPE})

    def decode(self, token: str) -> TokenClaims:
        payload = self._verify(token)
        # Los tokens emitidos antes de la administración del sistema no traen
        # alcance, y todos eran de restaurante.
        if payload.get("scope", RESTAURANT_SCOPE) != RESTAURANT_SCOPE:
            raise InvalidToken("La credencial no es de una cuenta del restaurante.")
        return self._to_claims(payload)

    def decode_platform(self, token: str) -> PlatformClaims:
        payload = self._verify(token)
        if payload.get("scope") != PLATFORM_SCOPE:
            raise InvalidToken("La credencial no es de la administración del sistema.")
        return PlatformClaims(admin_id=self._subject(payload))

    def _sign(self, claims: dict[str, object], ttl_seconds: int | None = None) -> AccessToken:
        ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        issued_at = datetime.now(UTC)
        payload = {
            **claims,
            "iat": issued_at,
            "exp": issued_at + timedelta(seconds=ttl),
        }
        token = jwt.encode(payload, self._secret_key, algorithm=self._algorithm)
        return AccessToken(value=token, expires_in_seconds=ttl)

    def _verify(self, token: str) -> dict[str, object]:
        try:
            return jwt.decode(token, self._secret_key, algorithms=[self._algorithm])
        except jwt.PyJWTError as error:
            raise InvalidToken() from error

    @staticmethod
    def _subject(payload: dict[str, object]) -> int:
        raw_subject = payload.get("sub")
        if not isinstance(raw_subject, str):
            raise InvalidToken("El token no trae sujeto.")
        try:
            return int(raw_subject)
        except ValueError as error:
            raise InvalidToken("El token trae un sujeto desconocido.") from error

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
            raise InvalidToken("El token no trae sujeto ni restaurante.")
        preview = payload.get("preview", False)
        # Estricto: un valor que no sea exactamente `true` o `false` es un token
        # que este servidor no emitió.
        if not isinstance(preview, bool):
            raise InvalidToken("El token trae una marca de vista previa desconocida.")
        admin_id = payload.get("platform_admin_id")
        if preview and (not isinstance(admin_id, int) or isinstance(admin_id, bool)):
            raise InvalidToken("El token de vista previa no dice quién la abrió.")
        return TokenClaims(
            user_id=cls._subject(payload),
            restaurant_id=raw_restaurant,
            preview=preview,
            platform_admin_id=admin_id if preview else None,
        )
