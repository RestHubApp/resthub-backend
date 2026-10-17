"""Puerto hacia el último uso de cada cuenta (RFS-24).

Cuándo usó alguien el sistema por última vez no lo guarda `users`: lo dicen las
sesiones de sus dispositivos y el historial de accesos, que son del núcleo.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Protocol


class AccountActivityReader(Protocol):
    async def last_activity(self, user_ids: Collection[int]) -> dict[int, datetime]:
        """El último uso de cada cuenta del personal; falta la que nunca entró."""
        ...
