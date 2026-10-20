from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.cpu_bound import run_cpu_bound
from resthub.modules.accounts.domain.entities import validate_new_password
from resthub.modules.accounts.domain.exceptions import (
    PreviewSessionRestrictedError,
    TemporaryPasswordExpiredError,
    UserNotFoundError,
    WrongCurrentPasswordError,
)
from resthub.modules.accounts.ports.restaurant_directory import RestaurantDirectory
from resthub.modules.accounts.ports.user_repository import PasswordHasher, UserRepository
from resthub.modules.accounts.use_cases.manage_staff import password_context


@dataclass(frozen=True, slots=True)
class ChangeOwnPasswordCommand:
    user_id: int
    current_password: str
    new_password: str
    # La sesión es una vista previa de la administración del sistema.
    preview: bool = False


class ChangeOwnPassword:
    """Cada cuenta cambia su contraseña, sabiendo la actual.

    Pedir la actual es lo que impide que alguien que encuentra una sesión
    abierta en el celular del local se quede con la cuenta.
    """

    def __init__(
        self,
        users: UserRepository,
        hasher: PasswordHasher,
        activity: ActivityRecorder,
        clock: Clock,
        *,
        restaurants: RestaurantDirectory,
        temporary_password_lifetime: timedelta,
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._activity = activity
        self._clock = clock
        self._restaurants = restaurants
        self._temporary_password_lifetime = temporary_password_lifetime

    async def __call__(self, command: ChangeOwnPasswordCommand) -> None:
        # Quien mira no es el dueño de la cuenta: no tiene contraseña que cambiar.
        if command.preview:
            raise PreviewSessionRestrictedError
        user = await self._users.get(command.user_id)
        if user is None:
            raise UserNotFoundError(command.user_id)
        matches, _ = await run_cpu_bound(
            self._hasher.verify_and_update, command.current_password, user.password_hash
        )
        if not matches:
            raise WrongCurrentPasswordError
        # Una temporal vencida no sirve ni para elegir la propia (ASVS 6.4.1).
        if user.temporary_password_expired(self._clock.now(), self._temporary_password_lifetime):
            raise TemporaryPasswordExpiredError

        context = await password_context(
            self._restaurants, user.restaurant_id, user.email, user.full_name
        )
        user.set_own_password(
            await run_cpu_bound(
                self._hasher.hash, validate_new_password(command.new_password, context)
            ),
            self._clock.now(),
        )
        await self._users.save(user)
        await self._activity.record(
            user.restaurant_id, command.user_id, ActivityKind.PASSWORD_CHANGED
        )
