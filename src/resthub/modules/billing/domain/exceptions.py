"""Errores de dominio de los comprobantes. Los adaptadores los traducen a HTTP."""

from __future__ import annotations


class BillingError(Exception):
    """Raíz de los errores del módulo de comprobantes."""


class InvalidInvoice(BillingError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidBillingSettings(BillingError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvoiceNotFound(BillingError):
    """También cuando es de otro restaurante."""

    def __init__(self, invoice_id: int) -> None:
        super().__init__(f"No existe el comprobante {invoice_id}.")
        self.invoice_id = invoice_id


class PaidOrderNotFound(BillingError):
    """El pedido no existe, es de otro local o todavía no está pagado."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"El pedido {order_id} no existe o no está pagado.")
        self.order_id = order_id


class InvoiceNumberTaken(BillingError):
    def __init__(self) -> None:
        super().__init__("Otro comprobante tomó el mismo número a la vez. Vuelve a intentarlo.")


class OrderAlreadyInvoiced(BillingError):
    def __init__(self, code: str) -> None:
        super().__init__(f"El pedido ya tiene el comprobante {code}.")
        self.code = code
