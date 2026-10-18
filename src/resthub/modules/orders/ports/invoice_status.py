"""Si un pedido ya tiene su comprobante, para no anularle un cobro debajo.

Los comprobantes son de `billing` y `orders` no puede importarlo: declara la
pregunta y la raíz de composición la conecta con el repositorio de comprobantes.
"""

from __future__ import annotations

from typing import Protocol


class InvoiceStatusReader(Protocol):
    async def has_live_invoice(self, restaurant_id: int, order_id: int) -> bool:
        """Si el pedido tiene un comprobante emitido y no anulado."""
        ...
