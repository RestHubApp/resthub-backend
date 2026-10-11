"""Cableado del adaptador HTTP de clientes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.database import SessionDep
from resthub.modules.customers.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyCustomerHistoryReader,
    SqlAlchemyCustomerRepository,
)
from resthub.modules.customers.ports.customer_repository import (
    CustomerHistoryReader,
    CustomerRepository,
)
from resthub.modules.customers.ports.linked_records import LinkedRecordsDirectory


def get_customer_repository(session: SessionDep) -> CustomerRepository:
    return SqlAlchemyCustomerRepository(session)


def get_customer_history(session: SessionDep) -> CustomerHistoryReader:
    return SqlAlchemyCustomerHistoryReader(session)


def get_linked_records() -> LinkedRecordsDirectory:
    """Sin implementación propia: los pedidos y las reservas son de otros módulos.

    `main.py` la reemplaza por la de `wiring/customer_records.py`.
    """
    message = "Los pedidos y reservas del cliente no están conectados."
    raise NotImplementedError(message)


LinkedRecordsDep = Annotated[LinkedRecordsDirectory, Depends(get_linked_records)]
CustomerRepositoryDep = Annotated[CustomerRepository, Depends(get_customer_repository)]
CustomerHistoryDep = Annotated[CustomerHistoryReader, Depends(get_customer_history)]
