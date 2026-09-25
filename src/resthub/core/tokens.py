from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from resthub.core.identity import AccessToken, InvalidToken, Role, TokenClaims


class JwtTokenService:
    def __init__(self, secret_key: str, algorithm: str, ttl_seconds: int) -> None:
        self._secret_key = secret_key
        self._algorithm = algorithm
        self._ttl_seconds = ttl_seconds

    def issue(self, user_id: int, role: Role, restaurant_id: int) -> AccessToken:
        issued_at = datetime.now(UTC)
        payload = {
            "sub": str(user_id),
            "role": role.value,
            # Viaja firmado para que el servidor lo compare contra la base: un
            # token emitido para un restaurante no sirve si la cuenta ya no está ahí.
            "restaurant_id": restaurant_id,
            "iat": issued_at,
            "exp": issued_at + timedelta(seconds=self._ttl_seconds),
        }
        token = jwt.encode(payload, self._secret_key, algorithm=self._algorithm)
        return AccessToken(value=token, expires_in_seconds=self._ttl_seconds)

    def decode(self, token: str) -> TokenClaims:
        try:
            payload = jwt.decode(token, self._secret_key, algorithms=[self._algorithm])
        except jwt.PyJWTError as error:
            raise InvalidToken() from error
        return self._to_claims(payload)

    @staticmethod
    def _to_claims(payload: dict[str, object]) -> TokenClaims:
        raw_subject = payload.get("sub")
        raw_role = payload.get("role")
        raw_restaurant = payload.get("restaurant_id")
        # `bool` es subclase de `int`: sin excluirlo, un `true` pasaría por restaurante.
        if (
            not isinstance(raw_subject, str)
            or not isinstance(raw_role, str)
            or not isinstance(raw_restaurant, int)
            or isinstance(raw_restaurant, bool)
        ):
            raise InvalidToken("El token no trae sujeto, rol ni restaurante.")
        try:
            return TokenClaims(
                user_id=int(raw_subject), role=Role(raw_role), restaurant_id=raw_restaurant
            )
        except ValueError as error:
            raise InvalidToken("El token trae un sujeto o un rol desconocido.") from error
