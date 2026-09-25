"""Adaptador del lector hacia la tabla ajena `menu_items`.

Consulta acotada a la pregunta del puerto. Se lee, nunca se escribe: el dueño
de la tabla es `menu`. Se describe con `table()` y `column()` sueltos, no con el
modelo ORM de `menu`, para no romper la independencia entre módulos y aun así
recibir el precio como `Decimal`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Integer, Numeric, Row, String, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.inventory.ports.dish_directory import Dish

_menu_items = table(
    "menu_items",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("category_id", Integer),
    column("name", String),
    column("price", Numeric(10, 2)),
    column("is_active", Boolean),
    column("position", Integer),
)
_menu_categories = table("menu_categories", column("id", Integer), column("position", Integer))


def _dish(row: Row[Any]) -> Dish:
    return Dish(id=int(row.id), name=str(row.name), price=row.price, is_active=bool(row.is_active))


class SqlDishDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, restaurant_id: int, menu_item_id: int) -> Dish | None:
        result = await self._session.execute(
            select(_menu_items).where(
                _menu_items.c.restaurant_id == restaurant_id, _menu_items.c.id == menu_item_id
            )
        )
        row = result.one_or_none()
        return _dish(row) if row is not None else None

    async def list_all(self, restaurant_id: int) -> list[Dish]:
        result = await self._session.execute(
            select(_menu_items)
            .join(_menu_categories, _menu_categories.c.id == _menu_items.c.category_id)
            .where(_menu_items.c.restaurant_id == restaurant_id)
            .order_by(_menu_categories.c.position, _menu_items.c.position, _menu_items.c.id)
        )
        return [_dish(row) for row in result]
