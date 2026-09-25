"""Pruebas del caso de uso de acceso, sin base de datos ni servidor."""

from __future__ import annotations

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.identity import AccessToken, Role, TokenClaims
from resthub.core.permissions import Permission
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import (
    InactiveAccount,
    InactiveRestaurant,
    InvalidCredentials,
)
from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary
from resthub.modules.accounts.use_cases.authenticate_user import (
    AuthenticateUser,
    AuthenticateUserCommand,
)
from tests.conftest import RecordingActivity
from tests.fakes import FakeHasher, InMemoryRestaurantDirectory, InMemoryUserRepository

PASSWORD = "contrasena-larga"
LOCAL = RestaurantSummary(id=5, name="Doña Rosa", slug="dona-rosa", is_active=True)


class FakeTokenService:
    def __init__(self) -> None:
        self.issued: list[TokenClaims] = []

    def issue(self, user_id: int, role: Role, restaurant_id: int) -> AccessToken:
        self.issued.append(TokenClaims(user_id=user_id, role=role, restaurant_id=restaurant_id))
        return AccessToken(value=f"token-{user_id}", expires_in_seconds=3600)

    def decode(self, token: str) -> TokenClaims:
        return self.issued[-1]


class CountingHasher(FakeHasher):
    """Registra cada verificación para poder afirmar que siempre ocurre."""

    def __init__(self) -> None:
        self.verifications = 0

    def verify(self, plain_password: str, password_hash: str) -> bool:
        self.verifications += 1
        return super().verify(plain_password, password_hash)


def _account(email: str = "ana@example.com", is_active: bool = True) -> User:
    return User(
        restaurant_id=LOCAL.id,
        email=email,
        full_name="Ana Quispe",
        role=Role.WAITER,
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
    restaurant: RestaurantSummary = LOCAL,
) -> AuthenticateUser:
    return AuthenticateUser(
        users,
        InMemoryRestaurantDirectory(restaurant),
        hasher or FakeHasher(),
        tokens or FakeTokenService(),
        activity or RecordingActivity(),
    )


async def test_las_credenciales_correctas_emiten_un_token_con_restaurante() -> None:
    tokens = FakeTokenService()
    activity = RecordingActivity()

    result = await _use_case(await _users_with(_account()), tokens=tokens, activity=activity)(
        AuthenticateUserCommand(email="Ana@Example.com", password=PASSWORD)
    )

    assert result.token.value == "token-1"
    assert tokens.issued == [TokenClaims(user_id=1, role=Role.WAITER, restaurant_id=LOCAL.id)]
    assert result.session.restaurant == LOCAL
    assert Permission.ORDERS_TAKE in result.session.permissions
    assert Permission.STAFF_MANAGE not in result.session.permissions
    assert activity.entries == [(LOCAL.id, 1, ActivityKind.SIGNED_IN, "")]


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("ana@example.com", "otra-contrasena"),
        ("nadie@example.com", PASSWORD),
        ("no-es-un-correo", PASSWORD),
    ],
)
async def test_toda_credencial_fallida_da_el_mismo_error(email: str, password: str) -> None:
    with pytest.raises(InvalidCredentials):
        await _use_case(await _users_with(_account()))(
            AuthenticateUserCommand(email=email, password=password)
        )


@pytest.mark.parametrize("email", ["ana@example.com", "nadie@example.com", "no-es-un-correo"])
async def test_la_verificacion_corre_aunque_el_correo_no_exista(email: str) -> None:
    """Sin esto, un correo desconocido responde antes y delata qué cuentas hay."""
    hasher = CountingHasher()

    with pytest.raises(InvalidCredentials):
        await _use_case(await _users_with(_account()), hasher=hasher)(
            AuthenticateUserCommand(email=email, password="otra-contrasena")
        )

    assert hasher.verifications == 1


async def test_una_cuenta_desactivada_no_accede() -> None:
    with pytest.raises(InactiveAccount):
        await _use_case(await _users_with(_account(is_active=False)))(
            AuthenticateUserCommand(email="ana@example.com", password=PASSWORD)
        )


async def test_una_cuenta_de_un_restaurante_desactivado_no_accede() -> None:
    cerrado = RestaurantSummary(id=LOCAL.id, name="Cerrado", slug="cerrado", is_active=False)
    tokens = FakeTokenService()

    with pytest.raises(InactiveRestaurant):
        await _use_case(await _users_with(_account()), tokens=tokens, restaurant=cerrado)(
            AuthenticateUserCommand(email="ana@example.com", password=PASSWORD)
        )
    assert tokens.issued == []


async def test_con_restaurante_desactivado_la_contrasena_mala_sigue_siendo_credencial_mala() -> (
    None
):
    """Decir "restaurante desactivado" antes de verificar confirmaría que el correo existe."""
    cerrado = RestaurantSummary(id=LOCAL.id, name="Cerrado", slug="cerrado", is_active=False)

    with pytest.raises(InvalidCredentials):
        await _use_case(await _users_with(_account()), restaurant=cerrado)(
            AuthenticateUserCommand(email="ana@example.com", password="otra-contrasena")
        )
