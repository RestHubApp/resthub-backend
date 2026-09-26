"""Puerto hacia la regla del local sobre descuentos."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class DiscountPolicy(Protocol):
    async def waiter_limit(self, restaurant_id: int) -> Decimal:
        """El descuento máximo, en porcentaje, que puede dar un mesero."""
        ...
