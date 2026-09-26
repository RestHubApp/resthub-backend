"""Bitácora de la administración del sistema.

Aparte de la de cada restaurante (`core/activity.py`): esa exige un local y una
cuenta del personal, y lo que hace la plataforma no ocurre dentro de ningún
local. Como la de los restaurantes, no se edita ni se borra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from resthub.core.pagination import Page, PageRequest
from resthub.modules.platform.domain.entities import PlatformActivityKind, PlatformActivityRecord


@dataclass(frozen=True, slots=True)
class PlatformActivityEntry:
    record: PlatformActivityRecord
    admin_name: str


class PlatformActivityLog(Protocol):
    async def record(self, admin_id: int, kind: PlatformActivityKind, detail: str = "") -> None:
        """Parte de la transacción: si la operación se deshace, el asiento también."""
        ...

    async def search(self, page: PageRequest) -> Page[PlatformActivityEntry]:
        """Lo más reciente primero."""
        ...
