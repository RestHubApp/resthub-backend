"""Volver a confirmar la contraseña antes de una acción crítica (RFS-21)."""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.cpu_bound import run_cpu_bound
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import (
    PreviewSessionRestrictedError,
    UserNotFoundError,
    WrongCurrentPasswordError,
)
from resthub.modules.accounts.ports.user_repository import PasswordHasher, UserRepository


@dataclass(frozen=True, slots=True)
class VerifyOwnPasswordCommand:
    user_id: int
    password: str
    # La sesión es una vista previa: quien mira no sabe la contraseña de la cuenta.
    preview: bool = False


class VerifyOwnPassword:
    """Comprueba la contraseña de la cuenta que ya tiene la sesión abierta.

    No cambia nada: quien llama decide qué marcar con la respuesta (la sesión
    reautenticada, un PIN nuevo, la verificación en dos pasos).
    """

    def __init__(self, users: UserRepository, hasher: PasswordHasher) -> None:
        self._users = users
        self._hasher = hasher

    async def __call__(self, command: VerifyOwnPasswordCommand) -> User:
        if command.preview:
            raise PreviewSessionRestrictedError
        user = await self._users.get(command.user_id)
        if user is None:
            raise UserNotFoundError(command.user_id)
        matches, _ = await run_cpu_bound(
            self._hasher.verify_and_update, command.password, user.password_hash
        )
        if not matches:
            raise WrongCurrentPasswordError
        return user
