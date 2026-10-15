"""Cableado del adaptador HTTP de mesas y pedidos."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.database import SessionDep
from resthub.modules.orders.adapters.persistence.directories import (
    SqlAlchemyConsumedItemsReader,
    SqlAlchemyCustomerDirectory,
    SqlAlchemyDiscountPolicy,
    SqlAlchemyMenuCatalogDirectory,
    SqlAlchemyRestaurantTimezoneReader,
    SqlAlchemyStaffDirectory,
)
from resthub.modules.orders.adapters.persistence.sqlalchemy_cash_register import (
    SqlAlchemyCashRegisterRepository,
)
from resthub.modules.orders.adapters.persistence.sqlalchemy_order_repository import (
    SqlAlchemyOrderRepository,
)
from resthub.modules.orders.adapters.persistence.sqlalchemy_table_repository import (
    SqlAlchemyTableRepository,
)
from resthub.modules.orders.ports.cash_register import CashRegisterRepository
from resthub.modules.orders.ports.consumed_items_reader import ConsumedItemsReader
from resthub.modules.orders.ports.customer_directory import CustomerDirectory
from resthub.modules.orders.ports.discount_policy import DiscountPolicy
from resthub.modules.orders.ports.menu_catalog import MenuCatalogDirectory
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.ports.restaurant_timezone_reader import RestaurantTimezoneReader
from resthub.modules.orders.ports.sent_to_kitchen_hook import SentOrder, SentToKitchenHook
from resthub.modules.orders.ports.served_order_hook import ServedOrder, ServedOrderHook
from resthub.modules.orders.ports.staff_directory import StaffDirectory
from resthub.modules.orders.ports.table_repository import TableRepository


def get_order_repository(session: SessionDep) -> OrderRepository:
    return SqlAlchemyOrderRepository(session)


def get_table_repository(session: SessionDep) -> TableRepository:
    return SqlAlchemyTableRepository(session)


def get_menu_catalog(session: SessionDep) -> MenuCatalogDirectory:
    return SqlAlchemyMenuCatalogDirectory(session)


def get_restaurant_timezone_reader(session: SessionDep) -> RestaurantTimezoneReader:
    return SqlAlchemyRestaurantTimezoneReader(session)


def get_staff_directory(session: SessionDep) -> StaffDirectory:
    return SqlAlchemyStaffDirectory(session)


def get_cash_register(session: SessionDep) -> CashRegisterRepository:
    return SqlAlchemyCashRegisterRepository(session)


def get_customer_directory(session: SessionDep) -> CustomerDirectory:
    return SqlAlchemyCustomerDirectory(session)


def get_discount_policy(session: SessionDep) -> DiscountPolicy:
    return SqlAlchemyDiscountPolicy(session)


def get_consumed_items_reader(session: SessionDep) -> ConsumedItemsReader:
    return SqlAlchemyConsumedItemsReader(session)


class NothingToConsume:
    """Servir un pedido no dispara nada fuera de este módulo."""

    async def order_served(self, served: ServedOrder) -> None:  # noqa: ARG002 - firma del puerto
        return None


def get_served_order_hook() -> ServedOrderHook:
    """Punto de conexión del aviso de pedido servido.

    Este módulo no sabe quién escucha, así que por sí solo no conecta a nadie.
    La raíz de composición (`main.py`) reemplaza esta dependencia por la que
    descuenta el inventario; un despliegue sin inventario funcionaría igual.
    """
    return NothingToConsume()


class NobodyInTheKitchen:
    """Enviar a cocina no dispara nada fuera de este módulo."""

    def order_sent(self, sent: SentOrder) -> None:  # noqa: ARG002 - firma del puerto
        return None


def get_sent_to_kitchen_hook() -> SentToKitchenHook:
    """Punto de conexión del aviso de platos que llegan a cocina.

    Igual que el de pedido servido: por sí solo no conecta a nadie, y
    `main.py` lo reemplaza por la clasificación de notas de `insights`.
    """
    return NobodyInTheKitchen()


OrderRepositoryDep = Annotated[OrderRepository, Depends(get_order_repository)]
TableRepositoryDep = Annotated[TableRepository, Depends(get_table_repository)]
MenuCatalogDep = Annotated[MenuCatalogDirectory, Depends(get_menu_catalog)]
RestaurantTimezoneReaderDep = Annotated[
    RestaurantTimezoneReader, Depends(get_restaurant_timezone_reader)
]
StaffDirectoryDep = Annotated[StaffDirectory, Depends(get_staff_directory)]
CashRegisterDep = Annotated[CashRegisterRepository, Depends(get_cash_register)]
DiscountPolicyDep = Annotated[DiscountPolicy, Depends(get_discount_policy)]
CustomerDirectoryDep = Annotated[CustomerDirectory, Depends(get_customer_directory)]
ConsumedItemsReaderDep = Annotated[ConsumedItemsReader, Depends(get_consumed_items_reader)]
ServedOrderHookDep = Annotated[ServedOrderHook, Depends(get_served_order_hook)]
SentToKitchenHookDep = Annotated[SentToKitchenHook, Depends(get_sent_to_kitchen_hook)]
