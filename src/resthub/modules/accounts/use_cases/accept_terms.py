"""Aceptar los términos y el tratamiento de datos personales (Ley N.º 29733).

Son dos consentimientos separados: cada uno guarda su versión y su fecha, y cada
uno deja su asiento en la bitácora.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import (
    PreviewSessionRestrictedError,
    UserNotFoundError,
)
from resthub.modules.accounts.ports.user_repository import UserRepository


@dataclass(frozen=True, slots=True)
class AcceptTermsCommand:
    user_id: int
    # Las versiones que la persona leyó: si alguna ya no es la vigente, no vale.
    terms_version: str
    privacy_version: str
    preview: bool = False


class AcceptTerms:
    """La cuenta acepta las versiones vigentes; cada consentimiento queda en la bitácora."""

    def __init__(self, users: UserRepository, activity: ActivityRecorder, clock: Clock) -> None:
        self._users = users
        self._activity = activity
        self._clock = clock

    async def __call__(self, command: AcceptTermsCommand) -> User:
        # Quien mira una vista previa no es el dueño de la cuenta: no acepta por él.
        if command.preview:
            raise PreviewSessionRestrictedError
        user = await self._users.get(command.user_id)
        if user is None:
            raise UserNotFoundError(command.user_id)
        now = self._clock.now()
        # Las dos se validan antes de guardar: o se aceptan ambas o ninguna.
        user.accept_terms(command.terms_version, now)
        user.accept_privacy(command.privacy_version, now)
        saved = await self._users.save(user)
        actor_id = saved.id or command.user_id
        await self._activity.record(
            saved.restaurant_id,
            actor_id,
            ActivityKind.TERMS_ACCEPTED,
            f"Versión {saved.terms_version}",
        )
        await self._activity.record(
            saved.restaurant_id,
            actor_id,
            ActivityKind.PRIVACY_ACCEPTED,
            f"Versión {saved.privacy_version}",
        )
        return saved
