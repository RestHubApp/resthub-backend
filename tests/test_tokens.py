"""El adaptador JWT: qué viaja en el token y qué se rechaza."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from resthub.core.identity import InvalidToken, PlatformClaims, TokenClaims
from resthub.core.tokens import JwtTokenService

SECRETO = "secreto-de-prueba-con-largo-suficiente"
TOKENS = JwtTokenService(secret_key=SECRETO, algorithm="HS256", ttl_seconds=60)


def _firmar(payload: dict[str, object], secreto: str = SECRETO) -> str:
    base: dict[str, object] = {
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=1),
    }
    return jwt.encode({**base, **payload}, secreto, algorithm="HS256")


def test_el_token_lleva_cuenta_y_restaurante_pero_no_rol() -> None:
    token = TOKENS.issue(7, 3)

    assert token.expires_in_seconds == 60
    assert TOKENS.decode(token.value) == TokenClaims(user_id=7, restaurant_id=3)
    assert "role" not in jwt.decode(token.value, SECRETO, algorithms=["HS256"])


@pytest.mark.parametrize("rol", ["waiter", "admin", "cook"])
def test_un_token_anterior_con_rol_sigue_sirviendo(rol: str) -> None:
    """El rol que traían los tokens viejos se ignora: los permisos salen de la base."""
    token = _firmar({"sub": "7", "role": rol, "restaurant_id": 3})

    assert TOKENS.decode(token) == TokenClaims(user_id=7, restaurant_id=3)


def test_un_token_sin_restaurante_se_rechaza() -> None:
    """Un token emitido antes de que existiera el restaurante no sirve."""
    with pytest.raises(InvalidToken):
        TOKENS.decode(_firmar({"sub": "7"}))


@pytest.mark.parametrize("restaurante", ["3", True, None, 3.5])
def test_un_restaurante_que_no_es_entero_se_rechaza(restaurante: object) -> None:
    with pytest.raises(InvalidToken):
        TOKENS.decode(_firmar({"sub": "7", "restaurant_id": restaurante}))


def test_un_sujeto_que_no_es_numero_se_rechaza() -> None:
    with pytest.raises(InvalidToken):
        TOKENS.decode(_firmar({"sub": "siete", "restaurant_id": 3}))


def test_una_firma_ajena_se_rechaza() -> None:
    ajeno = _firmar({"sub": "7", "restaurant_id": 3}, secreto="x" * 40)

    with pytest.raises(InvalidToken):
        TOKENS.decode(ajeno)


def test_un_token_vencido_se_rechaza() -> None:
    vencido = JwtTokenService(secret_key=SECRETO, algorithm="HS256", ttl_seconds=-1)

    with pytest.raises(InvalidToken):
        TOKENS.decode(vencido.issue(7, 3).value)


def test_el_token_de_restaurante_lleva_su_alcance() -> None:
    payload = jwt.decode(TOKENS.issue(7, 3).value, SECRETO, algorithms=["HS256"])

    assert payload["scope"] == "restaurant"


def test_un_token_de_restaurante_sin_alcance_sigue_sirviendo() -> None:
    """Los emitidos antes de la administración del sistema no traen `scope`."""
    assert TOKENS.decode(_firmar({"sub": "7", "restaurant_id": 3})) == TokenClaims(
        user_id=7, restaurant_id=3
    )


def test_el_token_de_plataforma_lleva_su_alcance_y_ningun_restaurante() -> None:
    token = TOKENS.issue_platform(4)
    payload = jwt.decode(token.value, SECRETO, algorithms=["HS256"])

    assert TOKENS.decode_platform(token.value) == PlatformClaims(admin_id=4)
    assert payload["scope"] == "platform"
    assert "restaurant_id" not in payload


def test_un_token_de_plataforma_no_sirve_como_de_restaurante() -> None:
    with pytest.raises(InvalidToken):
        TOKENS.decode(TOKENS.issue_platform(4).value)


def test_ni_aunque_traiga_un_restaurante() -> None:
    """El alcance manda: un restaurante agregado a mano no lo vuelve de restaurante."""
    with pytest.raises(InvalidToken):
        TOKENS.decode(_firmar({"sub": "4", "scope": "platform", "restaurant_id": 3}))


@pytest.mark.parametrize(
    "payload",
    [
        {"sub": "7", "restaurant_id": 3},
        {"sub": "7", "scope": "restaurant", "restaurant_id": 3},
        {"sub": "7", "scope": "otro"},
    ],
)
def test_un_token_que_no_es_de_plataforma_no_sirve_en_la_plataforma(
    payload: dict[str, object],
) -> None:
    with pytest.raises(InvalidToken):
        TOKENS.decode_platform(_firmar(payload))


def test_un_alcance_desconocido_no_sirve_en_el_restaurante() -> None:
    with pytest.raises(InvalidToken):
        TOKENS.decode(_firmar({"sub": "7", "scope": "otro", "restaurant_id": 3}))
