"""Adaptadores de lectores hacia tablas ajenas (`menu_items`, `restaurants`, `users`…).

Consultas acotadas a la pregunta de cada puerto. Se lee, nunca se escribe: los
dueños de esas tablas son otros módulos. Se describen con `table()` y
`column()` sueltos, no con los modelos ORM de sus dueños: importarlos rompería
la independencia entre módulos, y así igual se conservan los tipos (el precio
llega como `Decimal` también en SQLite).
"""

from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ColumnElement,
    Integer,
    Numeric,
    String,
    column,
    func,
    select,
    table,
)
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.crypto import get_cipher
from resthub.core.encrypted_types import EncryptedText
from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.modules.orders.domain.modifiers import DishOption, DishOptionGroup
from resthub.modules.orders.ports.customer_directory import KnownCustomer
from resthub.modules.orders.ports.menu_catalog import OrderableDish

_menu_items = table(
    "menu_items",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("name", String),
    column("price", Numeric(10, 2)),
    column("is_active", Boolean),
    column("is_available", Boolean),
    column("modifier_groups", JSON),
)
_recipe_lines = table(
    "recipe_lines",
    column("restaurant_id", Integer),
    column("menu_item_id", Integer),
    column("ingredient_id", Integer),
    column("quantity", Numeric(12, 3)),
)
_restaurants_flags = table(
    "restaurants", column("id", Integer), column("auto_out_of_stock", Boolean)
)
_stock_movements = table(
    "stock_movements",
    column("restaurant_id", Integer),
    column("ingredient_id", Integer),
    column("quantity", Numeric(12, 3)),
    column("kind", String),
    column("order_item_id", Integer),
)
# El valor de `MovementKind.CONSUMPTION` de `inventory`, que este módulo no importa.
_CONSUMPTION = "consumption"


def _groups(raw: list[dict[str, Any]] | None) -> tuple[DishOptionGroup, ...]:
    return tuple(
        DishOptionGroup(
            name=str(group["name"]),
            min_choices=int(group.get("min_choices", 0)),
            max_choices=int(group.get("max_choices", 1)),
            options=tuple(
                DishOption(name=str(option["name"]), price=Decimal(str(option["price"])))
                for option in group.get("options", [])
            ),
        )
        for group in raw or []
    )


_restaurants = table(
    "restaurants",
    column("id", Integer),
    column("timezone", String),
    column("max_waiter_discount_percent", Numeric(5, 2)),
)
# Si el local no aparece (no debería: el principal ya lo validó), el mesero no
# descuenta nada: el error seguro es el más restrictivo.
_NO_DISCOUNT = Decimal("0.00")
_users = table(
    "users", column("id", Integer), column("restaurant_id", Integer), column("full_name", String)
)


class SqlAlchemyMenuCatalogDirectory:
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
        out_of_stock_ids = await self._out_of_stock(restaurant_id, dish_ids)
        return {
            int(row.id): OrderableDish(
                id=int(row.id),
                name=str(row.name),
                price=row.price,
                is_active=bool(row.is_active),
                is_available=bool(row.is_available),
                modifier_groups=_groups(row.modifier_groups),
                out_of_stock=int(row.id) in out_of_stock_ids,
            )
            for row in result
        }

    async def _out_of_stock(self, restaurant_id: int, dish_ids: Collection[int]) -> set[int]:
        """De esos platos, los que no alcanzan para una porción según su receta."""
        is_active = await self._session.scalar(
            select(_restaurants_flags.c.auto_out_of_stock).where(
                _restaurants_flags.c.id == restaurant_id
            )
        )
        if not is_active:
            return set()
        stock = (
            select(
                _stock_movements.c.ingredient_id,
                func.sum(_stock_movements.c.quantity).label("on_hand"),
            )
            .where(_stock_movements.c.restaurant_id == restaurant_id)
            .group_by(_stock_movements.c.ingredient_id)
            .subquery()
        )
        result = await self._session.execute(
            select(_recipe_lines.c.menu_item_id)
            .outerjoin(stock, stock.c.ingredient_id == _recipe_lines.c.ingredient_id)
            .where(
                _recipe_lines.c.restaurant_id == restaurant_id,
                _recipe_lines.c.menu_item_id.in_(list(dish_ids)),
                func.coalesce(stock.c.on_hand, 0) < _recipe_lines.c.quantity,
            )
            .distinct()
        )
        return {int(item_id) for item_id in result.scalars()}


class SqlAlchemyConsumedItemsReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def consumed(self, restaurant_id: int, order_item_ids: Collection[int]) -> set[int]:
        if not order_item_ids:
            return set()
        result = await self._session.execute(
            select(_stock_movements.c.order_item_id)
            .where(
                _stock_movements.c.restaurant_id == restaurant_id,
                _stock_movements.c.order_item_id.in_(list(order_item_ids)),
                _stock_movements.c.kind == _CONSUMPTION,
            )
            .distinct()
        )
        return {int(item_id) for item_id in result.scalars() if item_id is not None}


class SqlAlchemyRestaurantTimezoneReader:
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


class SqlAlchemyStaffDirectory:
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


_customers = table(
    "customers",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("name", String),
    column("phone", EncryptedText),
    column("phone_key", String),
    column("address", EncryptedText),
    column("reference", EncryptedText),
)


class SqlAlchemyCustomerDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, restaurant_id: int, customer_id: int) -> KnownCustomer | None:
        return await self._one(
            _customers.c.id == customer_id, _customers.c.restaurant_id == restaurant_id
        )

    async def by_phone(self, restaurant_id: int, phone: str) -> KnownCustomer | None:
        key = phone.replace(" ", "")
        if not key:
            return None
        return await self._one(
            _customers.c.phone_key == get_cipher().blind_index(key),
            _customers.c.restaurant_id == restaurant_id,
        )

    async def _one(self, *conditions: ColumnElement[bool]) -> KnownCustomer | None:
        row = (
            await self._session.execute(select(_customers).where(*conditions).limit(1))
        ).one_or_none()
        if row is None:
            return None
        return KnownCustomer(
            id=int(row.id),
            name=str(row.name),
            phone=str(row.phone),
            address=str(row.address),
            reference=str(row.reference),
        )


class SqlAlchemyDiscountPolicy:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def waiter_limit(self, restaurant_id: int) -> Decimal:
        result = await self._session.execute(
            select(_restaurants.c.max_waiter_discount_percent).where(
                _restaurants.c.id == restaurant_id
            )
        )
        limit = result.scalar_one_or_none()
        return _NO_DISCOUNT if limit is None else Decimal(limit)
