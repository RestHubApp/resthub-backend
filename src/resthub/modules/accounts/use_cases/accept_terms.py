"""Aceptar los términos de uso y la política de privacidad (Ley N.º 29733)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import PreviewSessionRestricted, UserNotFound
from resthub.modules.accounts.ports.user_repository import UserRepository


@dataclass(frozen=True, slots=True)
class AcceptTermsCommand:
    user_id: int
    # La versión que la persona leyó: si ya no es la vigente, no vale.
    version: str
    preview: bool = False


class AcceptTerms:
    """La cuenta acepta la versión vigente; cada aceptación queda en la bitácora."""

    def __init__(self, users: UserRepository, activity: ActivityRecorder) -> None:
        self._users = users
        self._activity = activity

    async def __call__(self, command: AcceptTermsCommand) -> User:
        # Quien mira una vista previa no es el dueño de la cuenta: no acepta por él.
        if command.preview:
            raise PreviewSessionRestricted()
        user = await self._users.get(command.user_id)
        if user is None:
            raise UserNotFound(command.user_id)
        user.accept_terms(command.version, datetime.now(UTC))
        saved = await self._users.save(user)
        await self._activity.record(
            saved.restaurant_id,
            saved.id or command.user_id,
            ActivityKind.TERMS_ACCEPTED,
            f"Versión {saved.terms_version}",
        )
        return saved
