"""Caso de uso: la sesión vigente de una cuenta.

Reúne lo que la interfaz necesita para armarse: quién es, en qué restaurante
trabaja y qué puede hacer. Lo usan tanto `/auth/login` como `/auth/me`, así la
respuesta tiene la misma forma en los dos.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.permissions import Permission, permissions_for
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import UserNotFound
from resthub.modules.accounts.ports.restaurant_directory import (
    RestaurantDirectory,
    RestaurantSummary,
)
from resthub.modules.accounts.ports.user_repository import UserRepository


@dataclass(frozen=True, slots=True)
class CurrentSession:
    user: User
    restaurant: RestaurantSummary
    permissions: frozenset[Permission]


async def build_session(user: User, restaurants: RestaurantDirectory) -> CurrentSession:
    restaurant = await restaurants.get(user.restaurant_id)
    if restaurant is None:
        # La clave foránea lo impide; si pasa, la cuenta quedó huérfana y lo
        # correcto es tratarla como inexistente.
        raise UserNotFound(user.id or 0)
    return CurrentSession(user=user, restaurant=restaurant, permissions=permissions_for(user.role))


class ReadCurrentSession:
    def __init__(self, users: UserRepository, restaurants: RestaurantDirectory) -> None:
        self._users = users
        self._restaurants = restaurants

    async def __call__(self, user_id: int) -> CurrentSession:
        user = await self._users.get(user_id)
        if user is None:
            raise UserNotFound(user_id)
        return await build_session(user, self._restaurants)
