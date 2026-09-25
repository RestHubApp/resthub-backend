"""Adaptadores de lectores hacia tablas ajenas (`menu_items`, `restaurants`, `users`).

Consultas acotadas a la pregunta de cada puerto. Se lee, nunca se escribe: los
dueños de esas tablas son otros módulos. Se describen con `table()` y
`column()` sueltos, no con los modelos ORM de sus dueños: importarlos rompería
la independencia entre módulos, y así igual se conservan los tipos (el precio
llega como `Decimal` también en SQLite).
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import Boolean, Integer, Numeric, String, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.modules.orders.ports.menu_catalog import OrderableDish

_menu_items = table(
    "menu_items",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("name", String),
    column("price", Numeric(10, 2)),
    column("is_active", Boolean),
    column("is_available", Boolean),
)
_restaurants = table("restaurants", column("id", Integer), column("timezone", String))
_users = table(
    "users", column("id", Integer), column("restaurant_id", Integer), column("full_name", String)
)


class SqlMenuCatalog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_dishes(
        self, restaurant_id: int, dish_ids: Collection[int]
    ) -> dict[int, OrderableDish]:
        if not dish_ids:
            return {}
        result = await self._session.execute(
            select(_menu_items).where(
                _menu_items.c.restaurant_id == restaurant_id,
                _menu_items.c.id.in_(list(dish_ids)),
            )
        )
        return {
            int(row.id): OrderableDish(
                id=int(row.id),
                name=str(row.name),
                price=row.price,
                is_active=bool(row.is_active),
                is_available=bool(row.is_available),
            )
            for row in result
        }


class SqlRestaurantClock:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def timezone_for_numbering(self, restaurant_id: int) -> str:
        # `FOR UPDATE` bloquea la fila del restaurante hasta el fin de la
        # transacción: es el turno para numerar. SQLite no lo entiende y el
        # compilador lo omite, pero ahí las escrituras ya van de a una.
        result = await self._session.execute(
            select(_restaurants.c.timezone)
            .where(_restaurants.c.id == restaurant_id)
            .with_for_update()
        )
        timezone = result.scalar_one_or_none()
        return str(timezone) if timezone else DEFAULT_TIMEZONE


class SqlStaffDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def names(self, restaurant_id: int, user_ids: Collection[int]) -> dict[int, str]:
        if not user_ids:
            return {}
        result = await self._session.execute(
            select(_users.c.id, _users.c.full_name).where(
                _users.c.restaurant_id == restaurant_id, _users.c.id.in_(list(user_ids))
            )
        )
        return {int(row.id): str(row.full_name) for row in result}
