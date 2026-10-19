"""Pedidos → comprobantes: si un pedido tiene su comprobante vigente.

`orders` lo pregunta antes de anular un cobro (puerto `InvoiceStatusReader`) y
no puede importar `billing`. Este adaptador responde con el repositorio de
comprobantes, en la sesión de la petición. `main.py` lo instala.
"""

from __future__ import annotations

from resthub.core.database import SessionDep
from resthub.modules.billing.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyInvoiceRepository,
)
from resthub.modules.orders.ports.invoice_status import InvoiceStatusReader


class BillingInvoiceStatus:
    def __init__(self, invoices: SqlAlchemyInvoiceRepository) -> None:
        self._invoices = invoices

    async def has_live_invoice(self, restaurant_id: int, order_id: int) -> bool:
        invoice = await self._invoices.for_order(restaurant_id, order_id)
        return invoice is not None and not invoice.is_voided


def get_order_invoice_status(session: SessionDep) -> InvoiceStatusReader:
    return BillingInvoiceStatus(SqlAlchemyInvoiceRepository(session))
