"""Pruebas del caso de uso de acceso, sin base de datos ni servidor."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.identity import AccessToken, TokenClaims
from resthub.core.permissions import Permission
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import (
    InactiveRestaurantError,
    InvalidCredentialsError,
    TemporaryPasswordExpiredError,
)
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary
from resthub.modules.accounts.use_cases.authenticate_user import (
    AuthenticateUser,
    AuthenticateUserCommand,
)
from tests.conftest import RecordingActivity
from tests.fakes import (
    FIXED_NOW,
    FakeHasher,
    FixedClock,
    InMemoryRestaurantDirectory,
    InMemoryUserRepository,
)

PASSWORD = "contrasena-larga"
RESTAURANT = RestaurantSummary(
    id=5, name="Doña Rosa", slug="dona-rosa", timezone="America/Lima", is_active=True
)


class FakeTokenService:
    def __init__(self) -> None:
        self.issued: list[TokenClaims] = []

    def issue(self, user_id: int, restaurant_id: int) -> AccessToken:
        self.issued.append(TokenClaims(user_id=user_id, restaurant_id=restaurant_id))
        return AccessToken(value=f"token-{user_id}", expires_in_seconds=3600)

    def decode(self, token: str) -> TokenClaims:
        return self.issued[-1]


class CountingHasher(FakeHasher):
    """Registra cada verificación para poder afirmar que siempre ocurre."""

    def __init__(self) -> None:
        self.verifications = 0

    def verify_and_update(self, plain_password: str, password_hash: str) -> tuple[bool, str | None]:
        self.verifications += 1
        return super().verify_and_update(plain_password, password_hash)


def _account(email: str = "ana@example.com", is_active: bool = True) -> User:
    return User(
        restaurant_id=RESTAURANT.id,
        email=email,
        full_name="Ana Quispe",
        role=replace(Role.waiter(RESTAURANT.id), id=2),
        password_hash=FakeHasher().hash(PASSWORD),
        is_active=is_active,
    )


async def _users_with(user: User) -> InMemoryUserRepository:
    users = InMemoryUserRepository()
    await users.add(user)
    return users


def _use_case(
    users: InMemoryUserRepository,
    tokens: FakeTokenService | None = None,
    hasher: FakeHasher | None = None,
    activity: RecordingActivity | None = None,
    restaurant: RestaurantSummary = RESTAURANT,
    clock: FixedClock | None = None,
) -> AuthenticateUser:
    return AuthenticateUser(
        users,
        InMemoryRestaurantDirectory(restaurant),
        hasher or FakeHasher(),
        tokens or FakeTokenService(),
        activity or RecordingActivity(),
        clock=clock or FixedClock(),
        temporary_password_lifetime=timedelta(hours=72),
    )


async def test_correct_credentials_issue_token_with_restaurant() -> None:
    tokens = FakeTokenService()
    activity = RecordingActivity()

    result = await _use_case(await _users_with(_account()), tokens=tokens, activity=activity)(
        AuthenticateUserCommand(email="Ana@Example.com", password=PASSWORD)
    )

    assert result.token.value == "token-1"
    assert tokens.issued == [TokenClaims(user_id=1, restaurant_id=RESTAURANT.id)]
    assert result.session.restaurant == RESTAURANT
    assert Permission.ORDERS_TAKE in result.session.permissions
    assert Permission.STAFF_MANAGE not in result.session.permissions
    assert activity.entries == [(RESTAURANT.id, 1, ActivityKind.SIGNED_IN, "")]


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("ana@example.com", "otra-contrasena"),
        ("nadie@example.com", PASSWORD),
        ("no-es-un-correo", PASSWORD),
    ],
)
async def test_every_failed_credential_gives_same_error(email: str, password: str) -> None:
    with pytest.raises(InvalidCredentialsError):
        await _use_case(await _users_with(_account()))(
            AuthenticateUserCommand(email=email, password=password)
        )


@pytest.mark.parametrize("email", ["ana@example.com", "nadie@example.com", "no-es-un-correo"])
async def test_verification_runs_even_if_email_does_not_exist(email: str) -> None:
    """Sin esto, un correo desconocido responde antes y delata qué cuentas hay."""
    hasher = CountingHasher()

    with pytest.raises(InvalidCredentialsError):
        await _use_case(await _users_with(_account()), hasher=hasher)(
            AuthenticateUserCommand(email=email, password="otra-contrasena")
        )

    assert hasher.verifications == 1


async def test_deactivated_account_neither_signs_in_nor_confirms_password() -> None:
    with pytest.raises(InvalidCredentialsError):
        await _use_case(await _users_with(_account(is_active=False)))(
            AuthenticateUserCommand(email="ana@example.com", password=PASSWORD)
        )


async def test_account_of_deactivated_restaurant_cannot_sign_in() -> None:
    closed = RestaurantSummary(
        id=RESTAURANT.id, name="Cerrado", slug="cerrado", timezone="America/Lima", is_active=False
    )
    tokens = FakeTokenService()

    with pytest.raises(InactiveRestaurantError):
        await _use_case(await _users_with(_account()), tokens=tokens, restaurant=closed)(
            AuthenticateUserCommand(email="ana@example.com", password=PASSWORD)
        )
    assert tokens.issued == []


async def test_with_deactivated_restaurant_bad_password_remains_bad_credential() -> None:
    """Decir "restaurante desactivado" antes de verificar confirmaría que el correo existe."""
    closed = RestaurantSummary(
        id=RESTAURANT.id, name="Cerrado", slug="cerrado", timezone="America/Lima", is_active=False
    )

    with pytest.raises(InvalidCredentialsError):
        await _use_case(await _users_with(_account()), restaurant=closed)(
            AuthenticateUserCommand(email="ana@example.com", password="otra-contrasena")
        )


@pytest.mark.parametrize(("hours", "expired"), [(71, False), (72, True)])
async def test_a_temporary_password_expires_after_its_lifetime(hours: int, expired: bool) -> None:
    account = _account()
    account.set_temporary_password(FakeHasher().hash(PASSWORD), FIXED_NOW)
    clock = FixedClock(FIXED_NOW + timedelta(hours=hours))
    use_case = _use_case(await _users_with(account), clock=clock)
    command = AuthenticateUserCommand(email="ana@example.com", password=PASSWORD)

    if expired:
        with pytest.raises(TemporaryPasswordExpiredError):
            await use_case(command)
    else:
        assert (await use_case(command)).session.user.must_change_password


async def test_a_temporary_password_from_the_signup_counts_from_the_creation() -> None:
    account = replace(_account(), must_change_password=True, created_at=FIXED_NOW)
    late = FixedClock(FIXED_NOW + timedelta(days=4))

    with pytest.raises(TemporaryPasswordExpiredError):
        await _use_case(await _users_with(account), clock=late)(
            AuthenticateUserCommand(email="ana@example.com", password=PASSWORD)
        )


async def test_an_expired_temporary_password_that_was_wrong_is_just_invalid() -> None:
    account = replace(_account(), must_change_password=True, created_at=FIXED_NOW)
    late = FixedClock(FIXED_NOW + timedelta(days=4))

    with pytest.raises(InvalidCredentialsError):
        await _use_case(await _users_with(account), clock=late)(
            AuthenticateUserCommand(email="ana@example.com", password="otra-cosa-larga")
        )
