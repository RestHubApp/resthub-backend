"""Notas de crédito electrónicas: cómo se anula ante SUNAT un comprobante aceptado.

Un comprobante aceptado no se borra ni se edita: se anula con una nota de
crédito de motivo 01 («Anulación de la operación», catálogo 09) por el mismo
total. La nota lleva serie y correlativo propios por local, con la letra del
comprobante que afecta (BC01-1 anula una boleta, FC01-1 una factura), y se
envía sola a SUNAT, como el comprobante: desde la RS 114-2019 las notas de una
boleta también pueden ir una por una, sin resumen diario.

El comprobante queda anulado solo cuando SUNAT acepta la nota. Si la nota queda
pendiente o rechazada, el comprobante sigue vigente y la nota se reenvía.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from resthub.core.clock import utc_now
from resthub.modules.billing.domain.exceptions import InvalidInvoiceError
from resthub.modules.billing.domain.invoices import InvoiceStatus


class CreditNoteReason(StrEnum):
    """Motivo de la nota, con el código del catálogo 09 de SUNAT."""

    VOID = "01"

    @property
    def label(self) -> str:
        return {"01": "Anulación de la operación"}[self.value]


@dataclass(slots=True)
class CreditNote:
    restaurant_id: int
    # El comprobante que anula.
    invoice_id: int
    series: str
    number: int
    # Lo que escribió el encargado: va a SUNAT como descripción del motivo.
    reason: str
    # El total del comprobante que anula (con IGV): una anulación lo revierte entero.
    total: Decimal
    issued_by: int
    reason_code: CreditNoteReason = CreditNoteReason.VOID
    status: InvoiceStatus = InvoiceStatus.PENDING
    # Como en el comprobante: el XML firmado, la constancia de SUNAT (CDR en base64) y
    # el documento enviado, con el que se arma el PDF. Cifrados en reposo.
    signed_xml: str = ""
    cdr_zip: str = ""
    document_payload: dict[str, Any] = field(default_factory=dict)
    provider_message: str = ""
    provider_response: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    issued_at: datetime = field(default_factory=utc_now)

    @property
    def code(self) -> str:
        """«BC01-3»: como se lee en la nota impresa."""
        return f"{self.series}-{self.number}"

    @property
    def has_xml(self) -> bool:
        return bool(self.signed_xml)

    @property
    def has_cdr(self) -> bool:
        return bool(self.cdr_zip)

    @property
    def has_pdf(self) -> bool:
        """Como el del comprobante: solo de una nota aceptada."""
        return self.status is InvoiceStatus.ACCEPTED and bool(self.document_payload)

    def record_result(
        self,
        status: InvoiceStatus,
        message: str,
        raw: dict[str, Any],
        *,
        signed_xml: str = "",
        cdr_zip: str = "",
        document_payload: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.provider_message = message[:300]
        self.provider_response = raw
        self.signed_xml = signed_xml
        self.cdr_zip = cdr_zip
        self.document_payload = document_payload or {}

    def ensure_resendable(self) -> None:
        if self.status is InvoiceStatus.ACCEPTED:
            message = f"La nota de crédito {self.code} ya fue aceptada por SUNAT."
            raise InvalidInvoiceError(message)
