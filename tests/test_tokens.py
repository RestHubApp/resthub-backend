"""El adaptador JWT: qué viaja en el token y qué se rechaza."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from resthub.core.identity import InvalidToken, Role, TokenClaims
from resthub.core.tokens import JwtTokenService

SECRETO = "secreto-de-prueba-con-largo-suficiente"
TOKENS = JwtTokenService(secret_key=SECRETO, algorithm="HS256", ttl_seconds=60)


def _firmar(payload: dict[str, object], secreto: str = SECRETO) -> str:
    base: dict[str, object] = {
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=1),
    }
    return jwt.encode({**base, **payload}, secreto, algorithm="HS256")


def test_el_token_lleva_cuenta_rol_y_restaurante() -> None:
    token = TOKENS.issue(7, Role.WAITER, 3)

    assert token.expires_in_seconds == 60
    assert TOKENS.decode(token.value) == TokenClaims(user_id=7, role=Role.WAITER, restaurant_id=3)


def test_un_token_sin_restaurante_se_rechaza() -> None:
    """Un token emitido antes de que existiera el restaurante no sirve."""
    with pytest.raises(InvalidToken):
        TOKENS.decode(_firmar({"sub": "7", "role": "waiter"}))


@pytest.mark.parametrize("restaurante", ["3", True, None, 3.5])
def test_un_restaurante_que_no_es_entero_se_rechaza(restaurante: object) -> None:
    with pytest.raises(InvalidToken):
        TOKENS.decode(_firmar({"sub": "7", "role": "waiter", "restaurant_id": restaurante}))


def test_un_rol_desconocido_se_rechaza() -> None:
    with pytest.raises(InvalidToken):
        TOKENS.decode(_firmar({"sub": "7", "role": "cook", "restaurant_id": 3}))


def test_una_firma_ajena_se_rechaza() -> None:
    ajeno = _firmar({"sub": "7", "role": "admin", "restaurant_id": 3}, secreto="x" * 40)

    with pytest.raises(InvalidToken):
        TOKENS.decode(ajeno)


def test_un_token_vencido_se_rechaza() -> None:
    vencido = JwtTokenService(secret_key=SECRETO, algorithm="HS256", ttl_seconds=-1)

    with pytest.raises(InvalidToken):
        TOKENS.decode(vencido.issue(7, Role.ADMIN, 3).value)
