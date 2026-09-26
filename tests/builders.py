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
    SqlAlchemyCashRegister,
)
from resthub.modules.orders.adapters.persistence.sqlalchemy_table_repository import (
    SqlAlchemyTableRepository,
)
from resthub.modules.orders.domain.cash import CashSession
from resthub.modules.orders.domain.tables import DiningTable


@dataclass(frozen=True, slots=True)
class Carta:
    """Un menú mínimo: dos fondos, una bebida, uno agotado y uno retirado."""

    lomo: int
    aji: int
    chicha: int
    agotado: int
    retirado: int
    mesa_1: int
    mesa_2: int


async def carta(session: AsyncSession, restaurant_id: int) -> Carta:
    menu = SqlAlchemyMenuRepository(session)
    fondos = await menu.add_category(MenuCategory(restaurant_id=restaurant_id, name="Fondos"))
    bebidas = await menu.add_category(
        MenuCategory(restaurant_id=restaurant_id, name="Bebidas", position=1)
    )

    async def plato(category: MenuCategory, name: str, price: str, **extra: bool) -> int:
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
    mesa_1 = await tables.add(DiningTable(restaurant_id=restaurant_id, label="1"))
    mesa_2 = await tables.add(DiningTable(restaurant_id=restaurant_id, label="2", position=1))

    resultado = Carta(
        lomo=await plato(fondos, "Lomo saltado", "28.00"),
        aji=await plato(fondos, "Ají de gallina", "22.00"),
        chicha=await plato(bebidas, "Chicha morada", "5.50"),
        agotado=await plato(fondos, "Ceviche", "30.00", is_available=False),
        retirado=await plato(fondos, "Seco de res", "25.00", is_active=False),
        mesa_1=mesa_1.id or 0,
        mesa_2=mesa_2.id or 0,
    )
    await session.commit()
    return resultado


async def caja_abierta(
    session: AsyncSession, restaurant_id: int, admin_id: int, inicial: str = "100.00"
) -> CashSession:
    """Un turno de caja abierto: sin él no se puede cobrar."""
    abierta = await SqlAlchemyCashRegister(session).open(
        CashSession(
            restaurant_id=restaurant_id, opened_by=admin_id, opening_amount=Decimal(inicial)
        )
    )
    await session.commit()
    return abierta
