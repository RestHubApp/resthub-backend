from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import AccessToken, TokenService
from resthub.modules.accounts.domain.entities import User, normalize_email
from resthub.modules.accounts.domain.exceptions import (
    AccountsError,
    InactiveAccount,
    InactiveRestaurant,
    InvalidCredentials,
    InvalidEmail,
)
from resthub.modules.accounts.ports.restaurant_directory import RestaurantDirectory
from resthub.modules.accounts.ports.user_repository import PasswordHasher, UserRepository
from resthub.modules.accounts.use_cases.read_session import CurrentSession, build_session


@dataclass(frozen=True, slots=True)
class AuthenticateUserCommand:
    email: str
    password: str


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session: CurrentSession
    token: AccessToken


class AuthenticateUser:
    def __init__(
        self,
        users: UserRepository,
        restaurants: RestaurantDirectory,
        hasher: PasswordHasher,
        tokens: TokenService,
        activity: ActivityRecorder,
    ) -> None:
        self._users = users
        self._restaurants = restaurants
        self._hasher = hasher
        self._tokens = tokens
        self._activity = activity

    async def __call__(self, command: AuthenticateUserCommand) -> AuthenticatedSession:
        user = await self._find(command.email)

        # La verificación corre siempre, incluso sin usuario, para que un correo
        # inexistente tarde lo mismo que una contraseña equivocada.
        password_hash = user.password_hash if user else self._hasher.dummy_hash()
        password_matches = self._hasher.verify(command.password, password_hash)

        if user is None or not password_matches:
            raise InvalidCredentials()
        if not user.is_active:
            raise InactiveAccount(user.email)
        if user.id is None:
            # El repositorio incumplió su contrato: solo devuelve usuarios ya
            # persistidos, y esos siempre tienen identificador.
            raise AccountsError(f"La cuenta {user.email!r} llegó sin identificador.")

        session = await build_session(user, self._restaurants)
        # Se comprueba después de la contraseña: decirlo antes le confirmaría a
        # cualquiera que ese correo existe.
        if not session.restaurant.is_active:
            raise InactiveRestaurant()

        await self._activity.record(user.restaurant_id, user.id, ActivityKind.SIGNED_IN)
        token = self._tokens.issue(user.id, user.role, user.restaurant_id)
        return AuthenticatedSession(session=session, token=token)

    async def _find(self, raw_email: str) -> User | None:
        try:
            email = normalize_email(raw_email)
        except InvalidEmail:
            # Un correo mal formado es una credencial equivocada, no un error
            # de validación: responder distinto revelaría el formato esperado.
            return None
        return await self._users.get_by_email(email)
