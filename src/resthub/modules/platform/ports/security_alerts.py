"""Lo que necesita la detección de picos: contar señales y guardar las alertas."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from resthub.modules.platform.domain.security_alerts import SecurityAlert, SpikeKind


class SecuritySignalReader(Protocol):
    async def count_since(self, since: datetime) -> dict[SpikeKind, int]:
        """Cuántas veces ocurrió cada señal desde `since`, en todo el sistema."""
        ...


class SecurityAlertRepository(Protocol):
    async def add(self, alert: SecurityAlert) -> SecurityAlert: ...

    async def last_raised(self, kind: SpikeKind) -> datetime | None:
        """Cuándo alertó por última vez esa señal; `None` si nunca."""
        ...

    async def recent(self, limit: int) -> list[SecurityAlert]:
        """Las más nuevas primero."""
        ...
