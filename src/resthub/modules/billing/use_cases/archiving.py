"""Respaldo del PDF, el XML y el CDR de un comprobante en el almacenamiento propio.

El XML y el CDR están guardados en el comprobante; el PDF lo arma el servicio de
facturación al momento.

Corre fuera de la petición que emitió el comprobante (ver `wiring/invoice_archive.py`),
así que un Drive caído no frena el cobro. El estado queda en el comprobante
(pendiente, hecho o fallido) y el encargado puede reintentarlo.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.clock import Clock
from resthub.core.local_time import local_date
from resthub.modules.billing.domain.exceptions import (
    ArchiveDisabledError,
    BillingProviderError,
    DocumentArchiveError,
    InvalidInvoiceError,
    InvoiceDocumentUnavailableError,
)
from resthub.modules.billing.domain.invoices import ArchiveStatus, Invoice
from resthub.modules.billing.ports.billing_ports import (
    BillingSettingsRepository,
    ElectronicInvoiceGateway,
    InvoiceRepository,
)
from resthub.modules.billing.ports.delivery_ports import (
    DocumentArchiveGateway,
    InvoiceArchiveHook,
)
from resthub.modules.billing.use_cases.invoicing import (
    DocumentFormat,
    document_filename,
    find_invoice,
    invoice_document,
)

_UNEXPECTED = "No se pudo respaldar el comprobante; reinténtalo."


def archive_folder(invoice: Invoice, legal_name: str, timezone: str) -> tuple[str, str, str]:
    """Restaurante / año / mes (de la fecha local de emisión)."""
    day = local_date(invoice.issued_at, timezone)
    restaurant = f"{legal_name or 'Restaurante'} [{invoice.restaurant_id}]"
    return restaurant, f"{day.year:04d}", f"{day.year:04d}-{day.month:02d}"


class RequestArchive:
    """El encargado pide (o reintenta) el respaldo de un comprobante."""

    def __init__(self, invoices: InvoiceRepository, hook: InvoiceArchiveHook) -> None:
        self._invoices = invoices
        self._hook = hook

    async def __call__(self, restaurant_id: int, invoice_id: int) -> Invoice:
        if not self._hook.is_enabled():
            raise ArchiveDisabledError
        invoice = await find_invoice(self._invoices, restaurant_id, invoice_id, for_update=True)
        if not invoice.has_documents_to_archive:
            message = f"El comprobante {invoice.code} aún no tiene documentos aceptados por SUNAT."
            raise InvalidInvoiceError(message)
        if invoice.archive_status is ArchiveStatus.DONE:
            message = f"El comprobante {invoice.code} ya está respaldado."
            raise InvalidInvoiceError(message)
        invoice.mark_archive_pending()
        saved = await self._invoices.save(invoice)
        self._hook.schedule(restaurant_id, invoice_id)
        return saved


@dataclass(frozen=True, slots=True)
class ArchiveJob:
    """Lo que hace falta para subir los documentos, ya leído: la subida no toca la base."""

    invoice: Invoice
    folder: tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class ArchiveOutcome:
    status: ArchiveStatus
    message: str


class PrepareArchive:
    """Paso 1, en una sesión corta: lee el comprobante sin bloquearlo.

    La subida (Lycet arma el PDF y Drive atiende una subida a la vez) puede tardar;
    si la sesión siguiera abierta, cada respaldo en espera retendría una conexión del
    pool y el bloqueo del comprobante, y una ráfaga de cobros dejaría sin conexiones
    al resto de las peticiones.
    """

    def __init__(
        self,
        *,
        invoices: InvoiceRepository,
        settings: BillingSettingsRepository,
        archive: DocumentArchiveGateway,
    ) -> None:
        self._invoices = invoices
        self._settings = settings
        self._archive = archive

    async def __call__(self, restaurant_id: int, invoice_id: int) -> ArchiveJob | None:
        """`None` si ya no hay nada que hacer."""
        invoice = await self._invoices.get(restaurant_id, invoice_id)
        if (
            invoice is None
            or invoice.archive_status is not ArchiveStatus.PENDING
            or not self._archive.is_enabled()
        ):
            return None
        config = await self._settings.get(restaurant_id)
        return ArchiveJob(invoice, archive_folder(invoice, config.legal_name, config.timezone))


class UploadArchive:
    """Paso 2, sin sesión: sube el XML, el CDR y el PDF. Nunca lanza: dice cómo terminó."""

    def __init__(
        self, *, invoicer: ElectronicInvoiceGateway, archive: DocumentArchiveGateway
    ) -> None:
        self._invoicer = invoicer
        self._archive = archive

    async def __call__(self, job: ArchiveJob) -> ArchiveOutcome:
        invoice = job.invoice
        formats = (
            (DocumentFormat.XML, invoice.has_xml),
            (DocumentFormat.CDR, invoice.has_cdr),
            (DocumentFormat.PDF, invoice.has_pdf),
        )
        try:
            for document_format, available in formats:
                if available:
                    document = await invoice_document(invoice, document_format, self._invoicer)
                    filename = document_filename(invoice, document_format)
                    await self._archive.store(job.folder, filename, document)
        except (
            DocumentArchiveError,
            BillingProviderError,
            InvoiceDocumentUnavailableError,
        ) as error:
            return ArchiveOutcome(ArchiveStatus.FAILED, error.reason)
        except Exception:  # noqa: BLE001 - el respaldo nunca tumba nada: queda «fallido»
            return ArchiveOutcome(ArchiveStatus.FAILED, _UNEXPECTED)
        return ArchiveOutcome(ArchiveStatus.DONE, "")


class RecordArchive:
    """Paso 3, en otra sesión corta: toma el comprobante y anota cómo terminó."""

    def __init__(self, *, invoices: InvoiceRepository, clock: Clock) -> None:
        self._invoices = invoices
        self._clock = clock

    async def __call__(
        self, restaurant_id: int, invoice_id: int, outcome: ArchiveOutcome
    ) -> Invoice | None:
        """`None` si el estado cambió durante la subida: no se pisa."""
        invoice = await self._invoices.get(restaurant_id, invoice_id, for_update=True)
        if invoice is None or invoice.archive_status is not ArchiveStatus.PENDING:
            return None
        invoice.record_archive(outcome.status, outcome.message, self._clock.now())
        return await self._invoices.save(invoice)
