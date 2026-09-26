from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from resthub.core.identity import (
    PLATFORM_SCOPE,
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

    def __init__(self, secret_key: str, algorithm: str, ttl_seconds: int) -> None:
        self._secret_key = secret_key
        self._algorithm = algorithm
        self._ttl_seconds = ttl_seconds

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

    def _sign(self, claims: dict[str, object]) -> AccessToken:
        issued_at = datetime.now(UTC)
        payload = {
            **claims,
            "iat": issued_at,
            "exp": issued_at + timedelta(seconds=self._ttl_seconds),
        }
        token = jwt.encode(payload, self._secret_key, algorithm=self._algorithm)
        return AccessToken(value=token, expires_in_seconds=self._ttl_seconds)

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
        return TokenClaims(user_id=cls._subject(payload), restaurant_id=raw_restaurant)
