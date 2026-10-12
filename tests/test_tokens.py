"""El adaptador JWT: qué viaja en el token y qué se rechaza."""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from resthub.core.clock import SystemClock
from resthub.core.identity import InvalidTokenError, PlatformClaims, TokenClaims
from resthub.core.tokens import JwtTokenService

SECRET = "secreto-de-prueba-con-largo-suficiente"
TOKENS = JwtTokenService(secret_key=SECRET, algorithm="HS256", ttl_seconds=60)


def _sign(payload: dict[str, object], secret: str = SECRET) -> str:
    base: dict[str, object] = {
        "iat": SystemClock().now(),
        "exp": SystemClock().now() + timedelta(minutes=1),
    }
    return jwt.encode({**base, **payload}, secret, algorithm="HS256")


def test_token_carries_account_and_restaurant_but_not_role() -> None:
    token = TOKENS.issue(7, 3)

    assert token.expires_in_seconds == 60
    assert TOKENS.decode(token.value) == TokenClaims(user_id=7, restaurant_id=3)
    assert "role" not in jwt.decode(token.value, SECRET, algorithms=["HS256"])


@pytest.mark.parametrize("role", ["waiter", "admin", "cook"])
def test_older_token_with_role_still_works(role: str) -> None:
    """El rol que traían los tokens viejos se ignora: los permisos salen de la base."""
    token = _sign({"sub": "7", "role": role, "restaurant_id": 3})

    assert TOKENS.decode(token) == TokenClaims(user_id=7, restaurant_id=3)


def test_token_without_restaurant_is_rejected() -> None:
    """Un token emitido antes de que existiera el restaurante no sirve."""
    with pytest.raises(InvalidTokenError):
        TOKENS.decode(_sign({"sub": "7"}))


@pytest.mark.parametrize("restaurant", ["3", True, None, 3.5])
def test_non_integer_restaurant_is_rejected(restaurant: object) -> None:
    with pytest.raises(InvalidTokenError):
        TOKENS.decode(_sign({"sub": "7", "restaurant_id": restaurant}))


def test_non_numeric_subject_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        TOKENS.decode(_sign({"sub": "siete", "restaurant_id": 3}))


def test_foreign_signature_is_rejected() -> None:
    foreign = _sign({"sub": "7", "restaurant_id": 3}, secret="x" * 40)

    with pytest.raises(InvalidTokenError):
        TOKENS.decode(foreign)


def test_expired_token_is_rejected() -> None:
    expired = JwtTokenService(secret_key=SECRET, algorithm="HS256", ttl_seconds=-1)

    with pytest.raises(InvalidTokenError):
        TOKENS.decode(expired.issue(7, 3).value)


def test_restaurant_token_carries_its_scope() -> None:
    payload = jwt.decode(TOKENS.issue(7, 3).value, SECRET, algorithms=["HS256"])

    assert payload["scope"] == "restaurant"


def test_restaurant_token_without_scope_still_works() -> None:
    """Los emitidos antes de la administración del sistema no traen `scope`."""
    assert TOKENS.decode(_sign({"sub": "7", "restaurant_id": 3})) == TokenClaims(
        user_id=7, restaurant_id=3
    )


def test_platform_token_carries_its_scope_and_no_restaurant() -> None:
    token = TOKENS.issue_platform(4)
    payload = jwt.decode(token.value, SECRET, algorithms=["HS256"])

    assert TOKENS.decode_platform(token.value) == PlatformClaims(admin_id=4)
    assert payload["scope"] == "platform"
    assert "restaurant_id" not in payload


def test_platform_token_is_not_valid_as_restaurant_token() -> None:
    with pytest.raises(InvalidTokenError):
        TOKENS.decode(TOKENS.issue_platform(4).value)


def test_not_even_if_it_carries_a_restaurant() -> None:
    """El alcance manda: un restaurante agregado a mano no lo vuelve de restaurante."""
    with pytest.raises(InvalidTokenError):
        TOKENS.decode(_sign({"sub": "4", "scope": "platform", "restaurant_id": 3}))


@pytest.mark.parametrize(
    "payload",
    [
        {"sub": "7", "restaurant_id": 3},
        {"sub": "7", "scope": "restaurant", "restaurant_id": 3},
        {"sub": "7", "scope": "otro"},
    ],
)
def test_non_platform_token_is_not_valid_on_the_platform(
    payload: dict[str, object],
) -> None:
    with pytest.raises(InvalidTokenError):
        TOKENS.decode_platform(_sign(payload))


def test_unknown_scope_is_not_valid_in_the_restaurant() -> None:
    with pytest.raises(InvalidTokenError):
        TOKENS.decode(_sign({"sub": "7", "scope": "otro", "restaurant_id": 3}))


# --- Vista previa -------------------------------------------------------------


def test_preview_token_is_restaurant_token_with_two_marks_and_short_life() -> None:
    token = TOKENS.issue_preview(7, 3, platform_admin_id=2)

    assert token.expires_in_seconds == 30 * 60
    assert TOKENS.decode(token.value) == TokenClaims(
        user_id=7, restaurant_id=3, preview=True, platform_admin_id=2
    )
    payload = jwt.decode(token.value, SECRET, algorithms=["HS256"])
    assert payload["scope"] == "restaurant"
    assert payload["exp"] - payload["iat"] == pytest.approx(30 * 60)


def test_regular_token_is_not_a_preview_token() -> None:
    claims = TOKENS.decode(TOKENS.issue(7, 3).value)

    assert (claims.preview, claims.platform_admin_id) == (False, None)


def test_preview_token_is_not_valid_on_the_platform() -> None:
    with pytest.raises(InvalidTokenError):
        TOKENS.decode_platform(TOKENS.issue_preview(7, 3, platform_admin_id=2).value)


@pytest.mark.parametrize("preview_mark", ["true", 1, None])
def test_non_boolean_preview_mark_is_rejected(preview_mark: object) -> None:
    with pytest.raises(InvalidTokenError):
        TOKENS.decode(
            _sign({"sub": "7", "restaurant_id": 3, "preview": preview_mark, "platform_admin_id": 2})
        )


@pytest.mark.parametrize("admin", [None, "2", True])
def test_preview_without_who_opened_it_is_rejected(admin: object) -> None:
    payload: dict[str, object] = {"sub": "7", "restaurant_id": 3, "preview": True}
    if admin is not None:
        payload["platform_admin_id"] = admin
    with pytest.raises(InvalidTokenError):
        TOKENS.decode(_sign(payload))
