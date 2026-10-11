"""Cableado del adaptador HTTP de clientes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.database import SessionDep
from resthub.modules.customers.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyCustomerHistory,
    SqlAlchemyCustomerRepository,
)
from resthub.modules.customers.ports.customer_repository import (
    CustomerHistory,
    CustomerRepository,
)
from resthub.modules.customers.ports.linked_records import LinkedRecords


def get_customer_repository(session: SessionDep) -> CustomerRepository:
    return SqlAlchemyCustomerRepository(session)


def get_customer_history(session: SessionDep) -> CustomerHistory:
    return SqlAlchemyCustomerHistory(session)


def get_linked_records() -> LinkedRecords:
    """Sin implementación propia: los pedidos y las reservas son de otros módulos.

    `main.py` la reemplaza por la de `wiring/customer_records.py`.
    """
    raise NotImplementedError("Los pedidos y reservas del cliente no están conectados.")


LinkedRecordsDep = Annotated[LinkedRecords, Depends(get_linked_records)]
CustomerRepositoryDep = Annotated[CustomerRepository, Depends(get_customer_repository)]
CustomerHistoryDep = Annotated[CustomerHistory, Depends(get_customer_history)]
