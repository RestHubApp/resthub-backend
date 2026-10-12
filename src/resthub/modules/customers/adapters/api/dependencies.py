"""Cableado del adaptador HTTP de clientes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.activity_log import ActivityReaderDep, ActivityRecorderDep
from resthub.core.clock_dependency import ClockDep
from resthub.core.config import get_settings
from resthub.core.database import SessionDep
from resthub.core.realtime_broker import EventPublisherDep
from resthub.modules.customers.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyCustomerHistoryReader,
    SqlAlchemyCustomerRepository,
)
from resthub.modules.customers.ports.customer_repository import (
    CustomerHistoryReader,
    CustomerRepository,
)
from resthub.modules.customers.ports.linked_records import LinkedRecordsDirectory
from resthub.modules.customers.use_cases.data_access import DataAccessMonitor, DataAccessPolicy


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


def get_data_access_monitor(
    recorder: ActivityRecorderDep,
    reader: ActivityReaderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> DataAccessMonitor:
    settings = get_settings()
    return DataAccessMonitor(
        recorder,
        reader,
        events,
        clock,
        DataAccessPolicy(
            max_cards=settings.dlp_alert_max_cards,
            window_minutes=settings.dlp_alert_window_minutes,
        ),
    )


LinkedRecordsDep = Annotated[LinkedRecordsDirectory, Depends(get_linked_records)]
CustomerRepositoryDep = Annotated[CustomerRepository, Depends(get_customer_repository)]
CustomerHistoryDep = Annotated[CustomerHistoryReader, Depends(get_customer_history)]
DataAccessMonitorDep = Annotated[DataAccessMonitor, Depends(get_data_access_monitor)]
