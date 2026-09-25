from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.modules.accounts.domain.entities import validate_new_password
from resthub.modules.accounts.domain.exceptions import UserNotFound, WrongCurrentPassword
from resthub.modules.accounts.ports.user_repository import PasswordHasher, UserRepository


@dataclass(frozen=True, slots=True)
class ChangeOwnPasswordCommand:
    user_id: int
    current_password: str
    new_password: str


class ChangeOwnPassword:
    """Cada cuenta cambia su contraseña, sabiendo la actual.

    Pedir la actual es lo que impide que alguien que encuentra una sesión
    abierta en el celular del local se quede con la cuenta.
    """

    def __init__(
        self, users: UserRepository, hasher: PasswordHasher, activity: ActivityRecorder
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._activity = activity

    async def __call__(self, command: ChangeOwnPasswordCommand) -> None:
        user = await self._users.get(command.user_id)
        if user is None:
            raise UserNotFound(command.user_id)
        if not self._hasher.verify(command.current_password, user.password_hash):
            raise WrongCurrentPassword()

        user.password_hash = self._hasher.hash(validate_new_password(command.new_password))
        await self._users.save(user)
        await self._activity.record(
            user.restaurant_id, command.user_id, ActivityKind.PASSWORD_CHANGED
        )
