"""Dobles en memoria para probar casos de uso sin base de datos."""

from __future__ import annotations

from dataclasses import replace

from resthub.core.pagination import Page
from resthub.core.permissions import RoleKind
from resthub.core.realtime import RealtimeEvent
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import RoleInUse, RoleNameTaken
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary
from resthub.modules.accounts.ports.user_repository import UserQuery


class FakeHasher:
    """Reversible a propósito: la prueba mira el caso de uso, no el cifrado."""

    def hash(self, plain_password: str) -> str:
        return f"hash:{plain_password}"

    def verify(self, plain_password: str, password_hash: str) -> bool:
        return password_hash == f"hash:{plain_password}"

    def dummy_hash(self) -> str:
        return "hash:inalcanzable"


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._rows: dict[int, User] = {}

    async def add(self, user: User) -> User:
        stored = replace(user, id=len(self._rows) + 1)
        self._rows[stored.id or 0] = stored
        return replace(stored)

    async def get(self, user_id: int) -> User | None:
        found = self._rows.get(user_id)
        return replace(found) if found else None

    async def get_in_restaurant(self, restaurant_id: int, user_id: int) -> User | None:
        found = self._rows.get(user_id)
        if found is None or found.restaurant_id != restaurant_id:
            return None
        return replace(found)

    async def get_by_email(self, email: str) -> User | None:
        found = next((user for user in self._rows.values() if user.email == email), None)
        return replace(found) if found else None

    async def exists_with_email(self, email: str) -> bool:
        return any(user.email == email for user in self._rows.values())

    async def search(self, query: UserQuery) -> Page[User]:
        items = [
            replace(user)
            for user in self._rows.values()
            if user.restaurant_id == query.restaurant_id
            and (query.ids is None or user.id in query.ids)
            and (query.role_ids is None or user.role.id in query.role_ids)
            and (query.is_active is None or user.is_active == query.is_active)
        ]
        return Page(items=items[query.offset : query.offset + query.limit], total=len(items))

    async def save(self, user: User) -> User:
        self._rows[user.id or 0] = replace(user)
        return replace(user)


class InMemoryRoleRepository:
    """Roles en memoria; cuenta miembros mirando el repositorio de cuentas."""

    def __init__(self, users: InMemoryUserRepository | None = None) -> None:
        self._rows: dict[int, Role] = {}
        self._users = users or InMemoryUserRepository()

    async def add(self, role: Role) -> Role:
        if await self.exists_with_name(role.restaurant_id, role.name_key):
            raise RoleNameTaken(role.name)
        stored = replace(role, id=len(self._rows) + 1)
        self._rows[stored.id or 0] = stored
        return replace(stored)

    async def get_in_restaurant(
        self, restaurant_id: int, role_id: int, *, for_update: bool = False
    ) -> Role | None:
        found = self._rows.get(role_id)
        if found is None or found.restaurant_id != restaurant_id:
            return None
        return replace(found)

    async def find_by_kind(self, restaurant_id: int, kind: RoleKind) -> Role | None:
        return next(
            (
                replace(role)
                for role in self._rows.values()
                if role.restaurant_id == restaurant_id and role.kind is kind
            ),
            None,
        )

    async def list_for_restaurant(self, restaurant_id: int) -> list[Role]:
        return [
            replace(role) for role in self._rows.values() if role.restaurant_id == restaurant_id
        ]

    async def exists_with_name(
        self, restaurant_id: int, name_key: str, *, excluding_id: int | None = None
    ) -> bool:
        return any(
            role.restaurant_id == restaurant_id
            and role.name_key == name_key
            and role.id != excluding_id
            for role in self._rows.values()
        )

    async def member_counts(self, restaurant_id: int) -> dict[int, int]:
        counts: dict[int, int] = {}
        for role_id in await self._member_role_ids(restaurant_id):
            counts[role_id] = counts.get(role_id, 0) + 1
        return counts

    async def member_ids(self, restaurant_id: int, role_id: int) -> frozenset[int]:
        page = await self._users.search(
            UserQuery(restaurant_id=restaurant_id, role_ids=frozenset({role_id}), limit=1000)
        )
        return frozenset(user.id or 0 for user in page.items)

    async def save(self, role: Role) -> Role:
        self._rows[role.id or 0] = replace(role)
        return replace(role)

    async def delete(self, role: Role) -> None:
        if await self.member_ids(role.restaurant_id, role.id or 0):
            raise RoleInUse(role.name)
        self._rows.pop(role.id or 0, None)

    async def _member_role_ids(self, restaurant_id: int) -> list[int]:
        page = await self._users.search(UserQuery(restaurant_id=restaurant_id, limit=1000))
        return [user.role.id or 0 for user in page.items]


class InMemoryRestaurantDirectory:
    def __init__(self, *restaurants: RestaurantSummary) -> None:
        self._rows = {restaurant.id: restaurant for restaurant in restaurants}

    async def get(self, restaurant_id: int) -> RestaurantSummary | None:
        return self._rows.get(restaurant_id)


class RecordingEvents:
    def __init__(self) -> None:
        self.published: list[RealtimeEvent] = []

    def publish(self, event: RealtimeEvent) -> None:
        self.published.append(event)
