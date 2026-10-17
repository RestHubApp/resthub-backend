"""Revisión de accesos: las cuentas activas que nadie usa (RFS-24).

Una cuenta que nadie usa hace meses (alguien que dejó el local y nadie dio de
baja) es una puerta abierta. El encargado revisa la lista y desactiva las que
sobran, con su motivo, por la ruta de siempre.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from resthub.core.clock import Clock
from resthub.core.pagination import MAX_PAGE_SIZE
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.ports.account_activity import AccountActivityReader
from resthub.modules.accounts.ports.user_repository import UserQuery, UserRepository

DEFAULT_UNUSED_DAYS = 60


@dataclass(frozen=True, slots=True)
class ListUnusedStaffQuery:
    restaurant_id: int
    days: int = DEFAULT_UNUSED_DAYS


@dataclass(frozen=True, slots=True)
class UnusedAccount:
    user: User
    # `None` si nunca entró: entonces cuenta desde el alta.
    last_activity_at: datetime | None
    unused_days: int


class ListUnusedStaff:
    """Las cuentas activas del local sin uso en `days` días, la más olvidada primero."""

    def __init__(
        self, users: UserRepository, activity: AccountActivityReader, clock: Clock
    ) -> None:
        self._users = users
        self._activity = activity
        self._clock = clock

    async def __call__(self, query: ListUnusedStaffQuery) -> list[UnusedAccount]:
        members = await self._active_members(query.restaurant_id)
        last = await self._activity.last_activity([user.id for user in members if user.id])
        now = self._clock.now()
        unused = []
        for user in members:
            since = user.unused_since(last.get(user.id or 0))
            if now - since >= timedelta(days=query.days):
                unused.append(UnusedAccount(user, last.get(user.id or 0), (now - since).days))
        return sorted(unused, key=lambda account: -account.unused_days)

    async def _active_members(self, restaurant_id: int) -> list[User]:
        members: list[User] = []
        while True:
            page = await self._users.search(
                UserQuery(
                    restaurant_id=restaurant_id,
                    is_active=True,
                    limit=MAX_PAGE_SIZE,
                    offset=len(members),
                )
            )
            members.extend(page.items)
            if not page.items or len(members) >= page.total:
                return members
