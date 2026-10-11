"""Errores de dominio de los comprobantes. Los adaptadores los traducen a HTTP."""

from __future__ import annotations

from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class BillingError(Exception):
    """Raíz de los errores del módulo de comprobantes."""

    code: ClassVar[ErrorCode]


class InvalidInvoiceError(BillingError):
    code = ErrorCode.INVALID_INVOICE

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidBillingSettingsError(BillingError):
    code = ErrorCode.INVALID_BILLING_SETTINGS

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvoiceNotFoundError(BillingError):
    """También cuando es de otro restaurante."""

    code = ErrorCode.INVOICE_NOT_FOUND

    def __init__(self, invoice_id: int) -> None:
        super().__init__(f"No existe el comprobante {invoice_id}.")
        self.invoice_id = invoice_id


class PaidOrderNotFoundError(BillingError):
    """El pedido no existe, es de otro local o todavía no está pagado."""

    code = ErrorCode.PAID_ORDER_NOT_FOUND

    def __init__(self, order_id: int) -> None:
        super().__init__(f"El pedido {order_id} no existe o no está pagado.")
        self.order_id = order_id


class InvoiceNumberTakenError(BillingError):
    code = ErrorCode.INVOICE_NUMBER_TAKEN

    def __init__(self) -> None:
        super().__init__("Otro comprobante tomó el mismo número a la vez. Vuelve a intentarlo.")


class OrderAlreadyInvoicedError(BillingError):
    code = ErrorCode.ORDER_ALREADY_INVOICED

    def __init__(self, invoice_code: str) -> None:
        super().__init__(f"El pedido ya tiene el comprobante {invoice_code}.")
        self.invoice_code = invoice_code
