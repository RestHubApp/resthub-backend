"""El PIN que desbloquea la sesión tras la inactividad (RFS-29).

El celular del local queda sobre la barra: a los minutos sin uso que fije el
local, la sesión se bloquea y se retoma con un PIN corto en lugar de la
contraseña. El PIN se guarda con el mismo hash que la contraseña. Quien no
eligió PIN desbloquea con la contraseña.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.cpu_bound import run_cpu_bound
from resthub.modules.accounts.domain.entities import User, validate_pin
from resthub.modules.accounts.domain.exceptions import (
    PinNotSetError,
    PreviewSessionRestrictedError,
    UserNotFoundError,
)
from resthub.modules.accounts.ports.user_repository import PasswordHasher, UserRepository


async def _own_account(users: UserRepository, user_id: int, *, preview: bool) -> User:
    # Quien mira una vista previa no es el dueño de la cuenta: no elige su PIN.
    if preview:
        raise PreviewSessionRestrictedError
    user = await users.get(user_id)
    if user is None:
        raise UserNotFoundError(user_id)
    return user


@dataclass(frozen=True, slots=True)
class SetOwnPinCommand:
    user_id: int
    # `None` lo quita: la sesión bloqueada se retoma con la contraseña.
    pin: str | None
    preview: bool = False


class SetOwnPin:
    def __init__(
        self, users: UserRepository, hasher: PasswordHasher, activity: ActivityRecorder
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._activity = activity

    async def __call__(self, command: SetOwnPinCommand) -> User:
        user = await _own_account(self._users, command.user_id, preview=command.preview)
        if command.pin is None:
            user.pin_hash = None
        else:
            user.pin_hash = await run_cpu_bound(self._hasher.hash, validate_pin(command.pin))
        saved = await self._users.save(user)
        await self._activity.record(saved.restaurant_id, command.user_id, ActivityKind.PIN_CHANGED)
        return saved


@dataclass(frozen=True, slots=True)
class VerifyUnlockSecretCommand:
    user_id: int
    # Uno de los dos: el PIN si la cuenta tiene uno, la contraseña si no.
    pin: str | None = None
    password: str | None = None


class VerifyUnlockSecret:
    """Si el PIN (o, sin PIN, la contraseña) es el de la cuenta.

    No cuenta los intentos: los cuenta la sesión, que se cierra al tercero.
    """

    def __init__(self, users: UserRepository, hasher: PasswordHasher) -> None:
        self._users = users
        self._hasher = hasher

    async def __call__(self, command: VerifyUnlockSecretCommand) -> bool:
        user = await _own_account(self._users, command.user_id, preview=False)
        if command.pin is not None:
            if user.pin_hash is None:
                raise PinNotSetError
            secret, stored = command.pin, user.pin_hash
        else:
            secret, stored = command.password or "", user.password_hash
        matches, _ = await run_cpu_bound(self._hasher.verify_and_update, secret, stored)
        return matches
