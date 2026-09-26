"""Lectores de tablas ajenas: recetas y libro de stock del inventario.

Consultas acotadas a la pregunta del puerto. Se lee, nunca se escribe, y se
describen con `table()` y `column()` sueltos para no importar el módulo dueño.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, Numeric, column, func, select, table
from sqlalchemy.ext.asyncio import AsyncSession

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
)


class SqlStockAvailability:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def out_of_stock(self, restaurant_id: int) -> frozenset[int]:
        activo = await self._session.scalar(
            select(_restaurants_flags.c.auto_out_of_stock).where(
                _restaurants_flags.c.id == restaurant_id
            )
        )
        if not activo:
            return frozenset()
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
                func.coalesce(stock.c.on_hand, 0) < _recipe_lines.c.quantity,
            )
            .distinct()
        )
        return frozenset(int(item_id) for item_id in result.scalars())
