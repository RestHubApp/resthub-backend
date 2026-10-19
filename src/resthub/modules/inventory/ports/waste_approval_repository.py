"""Puertos de las mermas por aprobar y del monto que las dispara."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.modules.inventory.domain.waste_approvals import WasteApproval, WasteApprovalStatus


@dataclass(frozen=True, slots=True)
class WasteApprovalQuery:
    restaurant_id: int
    status: WasteApprovalStatus | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class WasteApprovalRepository(Protocol):
    async def add(self, approval: WasteApproval) -> WasteApproval: ...

    async def get(
        self, restaurant_id: int, approval_id: int, *, for_update: bool = False
    ) -> WasteApproval | None: ...

    async def save(self, approval: WasteApproval) -> WasteApproval: ...

    async def search(self, query: WasteApprovalQuery) -> Page[WasteApproval]:
        """De la más reciente a la más antigua."""
        ...


class WasteApprovalPolicy(Protocol):
    """El ajuste del local que posee `restaurants`, leído por SQL."""

    async def threshold(self, restaurant_id: int) -> Decimal | None:
        """Desde qué monto, en soles, una merma pide aprobación; `None` si nunca."""
        ...
