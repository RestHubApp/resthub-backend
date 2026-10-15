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


class EmailSendingDisabledError(BillingError):
    code = ErrorCode.EMAIL_SENDING_DISABLED

    def __init__(self) -> None:
        super().__init__("El envío por correo no está configurado en este servidor.")


class EmailDeliveryError(BillingError):
    """El proveedor de correo no entregó el mensaje. Nunca lleva la dirección del cliente."""

    code = ErrorCode.EMAIL_DELIVERY

    def __init__(self, reason: str = "No se pudo enviar el correo. Inténtalo de nuevo.") -> None:
        super().__init__(reason)


class EmailRateLimitedError(BillingError):
    code = ErrorCode.EMAIL_RATE_LIMITED

    def __init__(self, limit: int) -> None:
        super().__init__(f"Llegaste al límite de {limit} correos por hora. Espera un momento.")
        self.limit = limit


class ArchiveDisabledError(BillingError):
    code = ErrorCode.ARCHIVE_DISABLED

    def __init__(self) -> None:
        super().__init__("El respaldo en Google Drive no está configurado en este servidor.")


class DocumentArchiveError(BillingError):
    """Falló descargar un documento del proveedor o subirlo al respaldo (sin datos personales)."""

    code = ErrorCode.DOCUMENT_ARCHIVE

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidCertificateError(BillingError):
    """El certificado digital no se pudo abrir: archivo dañado, contraseña errada o incompleto."""

    code = ErrorCode.INVALID_CERTIFICATE

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidBillingCredentialsError(BillingError):
    """El servicio de facturación rechazó el usuario SOL, la clave o el certificado del local."""

    code = ErrorCode.INVALID_BILLING_CREDENTIALS

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BillingProviderError(BillingError):
    """El servicio de facturación (Lycet) no está disponible: no responde o falla él mismo."""

    code = ErrorCode.BILLING_PROVIDER

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvoiceDocumentUnavailableError(BillingError):
    """El comprobante todavía no tiene ese documento (XML, CDR o PDF)."""

    code = ErrorCode.INVOICE_DOCUMENT_UNAVAILABLE

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
