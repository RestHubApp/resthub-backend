"""Puerto de persistencia de la caja: los turnos y lo que se cobró en cada uno."""

from __future__ import annotations

from typing import Protocol

from resthub.core.pagination import Page
from resthub.modules.orders.domain.cash import CashOrderAdjustment, CashPayment, CashSession


class CashRegister(Protocol):
    async def current(self, restaurant_id: int, *, for_update: bool = False) -> CashSession | None:
        """La caja abierta del local, o `None`.

        Con `for_update` queda tomada hasta el fin de la transacción: un cobro
        y el cierre no se cruzan, así el arqueo no deja afuera un pago.
        """
        ...

    async def get(self, restaurant_id: int, session_id: int) -> CashSession | None: ...

    async def open(self, session: CashSession) -> CashSession:
        """Guarda una caja nueva; `CashRegisterAlreadyOpen` si ya había una abierta."""
        ...

    async def save(self, session: CashSession) -> CashSession: ...

    async def history(self, restaurant_id: int, limit: int, offset: int) -> Page[CashSession]:
        """Los turnos, del más reciente al más antiguo."""
        ...

    async def payments(self, restaurant_id: int, session_id: int) -> list[CashPayment]: ...

    async def adjustments(self, restaurant_id: int, session_id: int) -> list[CashOrderAdjustment]:
        """Descuento y cortesías de cada pedido que tuvo un pago en el turno."""
        ...
