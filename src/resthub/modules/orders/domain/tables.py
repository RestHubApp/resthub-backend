"""Mesas del salón.

Python puro. El estado libre/ocupada no se guarda en la mesa: se deduce de si
tiene un pedido activo. Guardarlo aparte sería una segunda verdad que puede
quedar desfasada de la primera.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from resthub.modules.orders.domain.exceptions import InvalidTable, InvalidTableOrdering

MAX_LABEL_LENGTH = 30


class TableStatus(StrEnum):
    FREE = "free"
    OCCUPIED = "occupied"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


_STATUS_LABELS: dict[TableStatus, str] = {
    TableStatus.FREE: "Libre",
    TableStatus.OCCUPIED: "Ocupada",
}


@dataclass(slots=True)
class DiningTable:
    restaurant_id: int
    # Texto y no número: en un local chico conviven "5", "Terraza 2" y "Barra".
    label: str
    position: int = 0
    is_active: bool = True
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.label = validate_label(self.label)

    def relabel(self, label: str) -> None:
        self.label = validate_label(label)


def validate_label(raw: str) -> str:
    label = " ".join(raw.split())
    if not label:
        raise InvalidTable("El nombre de la mesa no puede quedar vacío.")
    if len(label) > MAX_LABEL_LENGTH:
        raise InvalidTable(f"El nombre de la mesa no puede pasar de {MAX_LABEL_LENGTH} caracteres.")
    return label


def reorder_tables(tables: Sequence[DiningTable], ids: Sequence[int]) -> list[DiningTable]:
    by_id = {table.id: table for table in tables}
    if len(ids) != len(by_id) or set(ids) != set(by_id):
        raise InvalidTableOrdering()
    ordered = [by_id[table_id] for table_id in ids]
    for position, table in enumerate(ordered):
        table.position = position
    return ordered
