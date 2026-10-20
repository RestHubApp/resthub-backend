from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.cpu_bound import run_cpu_bound
from resthub.core.identity import AccessToken, TokenService
from resthub.modules.accounts.domain.entities import User, normalize_email
from resthub.modules.accounts.domain.exceptions import (
    AccountsError,
    InactiveRestaurantError,
    InvalidCredentialsError,
    InvalidEmailError,
    TemporaryPasswordExpiredError,
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
        *,
        clock: Clock,
        temporary_password_lifetime: timedelta,
    ) -> None:
        self._users = users
        self._restaurants = restaurants
        self._hasher = hasher
        self._tokens = tokens
        self._activity = activity
        self._clock = clock
        self._temporary_password_lifetime = temporary_password_lifetime

    async def __call__(self, command: AuthenticateUserCommand) -> AuthenticatedSession:
        user = await self._find(command.email)

        # La verificación corre siempre, incluso sin usuario, para que un correo
        # inexistente tarde lo mismo que una contraseña equivocada. Va en un
        # hilo aparte: el hash ocupa la CPU unos cientos de milisegundos y, en
        # el bucle de eventos, frenaría a todas las demás peticiones. El
        # ejecutor tiene tope (`core/cpu_bound`) para que muchos accesos a la
        # vez no se lleven todos los núcleos.
        password_hash = user.password_hash if user else self._hasher.dummy_hash()
        password_matches, upgraded_hash = await run_cpu_bound(
            self._hasher.verify_and_update, command.password, password_hash
        )

        # Una cuenta desactivada responde igual que una contraseña equivocada,
        # como en la plataforma: otra respuesta confirmaría que la contraseña
        # era la correcta a quien la está adivinando.
        if user is None or not password_matches or not user.is_active:
            raise InvalidCredentialsError
        if user.id is None:
            # El repositorio incumplió su contrato: solo devuelve usuarios ya
            # persistidos, y esos siempre tienen identificador.
            message = f"La cuenta {user.email!r} llegó sin identificador."
            raise AccountsError(message)

        session = await build_session(user, self._restaurants)
        # Se comprueba después de la contraseña: decirlo antes le confirmaría a
        # cualquiera que ese correo existe.
        if not session.restaurant.is_active:
            raise InactiveRestaurantError
        # Al local de muestra solo se entra por la vista previa, ni siquiera
        # con una cuenta a la que alguien le puso contraseña desde adentro.
        if session.restaurant.is_sandbox:
            raise InvalidCredentialsError
        # La contraseña temporal sirve unos días (ASVS 6.4.1); pasado el plazo, la
        # cuenta pide otra a quien la gestiona. Se dice después de verificarla.
        if user.temporary_password_expired(self._clock.now(), self._temporary_password_lifetime):
            raise TemporaryPasswordExpiredError

        # El hash quedó atrás (bcrypt de antes, o un costo menor): con la contraseña recién
        # verificada se reemplaza, y la cuenta migra a Argon2id sin que nadie lo note.
        if upgraded_hash is not None:
            user.password_hash = upgraded_hash
            await self._users.save(user)
        await self._activity.record(user.restaurant_id, user.id, ActivityKind.SIGNED_IN)
        token = self._tokens.issue(user.id, user.restaurant_id)
        return AuthenticatedSession(session=session, token=token)

    async def _find(self, raw_email: str) -> User | None:
        try:
            email = normalize_email(raw_email)
        except InvalidEmailError:
            # Un correo mal formado es una credencial equivocada, no un error
            # de validación: responder distinto revelaría el formato esperado.
            return None
        return await self._users.get_by_email(email)
