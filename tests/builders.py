"""Datos de negocio armados directo en la base, para pruebas de pedidos e inventario.

Se escriben con los repositorios de cada módulo y no por HTTP: cada prueba
arranca con un menú y unas mesas sin pagar el costo de veinte peticiones.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.menu.adapters.persistence.sqlalchemy_menu_repository import (
    SqlAlchemyMenuRepository,
)
from resthub.modules.menu.domain.entities import MenuCategory, MenuItem
from resthub.modules.orders.adapters.persistence.sqlalchemy_cash_register import (
    SqlAlchemyCashRegisterRepository,
)
from resthub.modules.orders.adapters.persistence.sqlalchemy_table_repository import (
    SqlAlchemyTableRepository,
)
from resthub.modules.orders.domain.cash import CashSession
from resthub.modules.orders.domain.tables import DiningTable


@dataclass(frozen=True, slots=True)
class SeededMenu:
    """Un menú mínimo: dos fondos, una bebida, uno agotado y uno retirado."""

    lomo: int
    aji: int
    chicha: int
    sold_out: int
    withdrawn: int
    table_1: int
    table_2: int


async def seed_menu(session: AsyncSession, restaurant_id: int) -> SeededMenu:
    menu = SqlAlchemyMenuRepository(session)
    mains = await menu.add_category(MenuCategory(restaurant_id=restaurant_id, name="Fondos"))
    drinks = await menu.add_category(
        MenuCategory(restaurant_id=restaurant_id, name="Bebidas", position=1)
    )

    async def dish(category: MenuCategory, name: str, price: str, **extra: bool) -> int:
        created = await menu.add_item(
            MenuItem(
                restaurant_id=restaurant_id,
                category_id=category.id or 0,
                name=name,
                price=Decimal(price),
                **extra,
            )
        )
        return created.id or 0

    tables = SqlAlchemyTableRepository(session)
    table_1 = await tables.add(DiningTable(restaurant_id=restaurant_id, label="1"))
    table_2 = await tables.add(DiningTable(restaurant_id=restaurant_id, label="2", position=1))

    result = SeededMenu(
        lomo=await dish(mains, "Lomo saltado", "28.00"),
        aji=await dish(mains, "Ají de gallina", "22.00"),
        chicha=await dish(drinks, "Chicha morada", "5.50"),
        sold_out=await dish(mains, "Ceviche", "30.00", is_available=False),
        withdrawn=await dish(mains, "Seco de res", "25.00", is_active=False),
        table_1=table_1.id or 0,
        table_2=table_2.id or 0,
    )
    await session.commit()
    return result


async def open_cash_session(
    session: AsyncSession, restaurant_id: int, admin_id: int, opening_amount: str = "100.00"
) -> CashSession:
    """Un turno de caja abierto: sin él no se puede cobrar."""
    opened = await SqlAlchemyCashRegisterRepository(session).open(
        CashSession(
            restaurant_id=restaurant_id, opened_by=admin_id, opening_amount=Decimal(opening_amount)
        )
    )
    await session.commit()
    return opened
