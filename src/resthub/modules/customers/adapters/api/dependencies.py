"""Cableado del adaptador HTTP de clientes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.modules.customers.ports.linked_records import LinkedRecords


def get_linked_records() -> LinkedRecords:
    """Sin implementación propia: los pedidos y las reservas son de otros módulos.

    `main.py` la reemplaza por la de `wiring/customer_records.py`.
    """
    raise NotImplementedError("Los pedidos y reservas del cliente no están conectados.")


LinkedRecordsDep = Annotated[LinkedRecords, Depends(get_linked_records)]
