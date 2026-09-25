"""Dobles en memoria para probar casos de uso sin base de datos."""

from __future__ import annotations

from dataclasses import replace

from resthub.core.pagination import Page
from resthub.core.realtime import RealtimeEvent
from resthub.modules.accounts.domain.entities import User
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
            and (query.roles is None or user.role in query.roles)
            and (query.is_active is None or user.is_active == query.is_active)
        ]
        return Page(items=items[query.offset : query.offset + query.limit], total=len(items))

    async def save(self, user: User) -> User:
        self._rows[user.id or 0] = replace(user)
        return replace(user)


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
