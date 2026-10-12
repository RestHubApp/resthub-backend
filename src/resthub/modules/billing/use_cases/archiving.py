"""Respaldo del PDF, el XML y el CDR de un comprobante en el almacenamiento propio.

Corre fuera de la petición que emitió el comprobante (ver `wiring/invoice_archive.py`),
así que un Drive caído no frena el cobro. El estado queda en el comprobante
(pendiente, hecho o fallido) y el encargado puede reintentarlo.
"""

from __future__ import annotations

from resthub.core.clock import Clock
from resthub.core.local_time import local_date
from resthub.modules.billing.domain.exceptions import (
    ArchiveDisabledError,
    DocumentArchiveError,
    InvalidInvoiceError,
)
from resthub.modules.billing.domain.invoices import ArchiveStatus, Invoice
from resthub.modules.billing.ports.billing_ports import (
    BillingSettingsRepository,
    InvoiceRepository,
)
from resthub.modules.billing.ports.delivery_ports import (
    DocumentArchiveGateway,
    DocumentFetchGateway,
    InvoiceArchiveHook,
)
from resthub.modules.billing.use_cases.invoicing import find_invoice

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


class ArchiveInvoiceDocuments:
    def __init__(
        self,
        *,
        invoices: InvoiceRepository,
        settings: BillingSettingsRepository,
        fetcher: DocumentFetchGateway,
        archive: DocumentArchiveGateway,
        clock: Clock,
    ) -> None:
        self._invoices = invoices
        self._settings = settings
        self._fetcher = fetcher
        self._archive = archive
        self._clock = clock

    async def __call__(self, restaurant_id: int, invoice_id: int) -> Invoice | None:
        """Sube los documentos y anota el resultado. `None` si ya no hay nada que hacer."""
        invoice = await self._invoices.get(restaurant_id, invoice_id, for_update=True)
        if (
            invoice is None
            or invoice.archive_status is not ArchiveStatus.PENDING
            or not self._archive.is_enabled()
        ):
            return None
        config = await self._settings.get(restaurant_id)
        folder = archive_folder(invoice, config.legal_name, config.timezone)
        documents = (("pdf", invoice.pdf_url), ("xml", invoice.xml_url), ("zip", invoice.cdr_url))
        try:
            for extension, url in documents:
                if url:
                    document = await self._fetcher.fetch(url)
                    await self._archive.store(folder, f"{invoice.code}.{extension}", document)
        except DocumentArchiveError as error:
            invoice.record_archive(ArchiveStatus.FAILED, error.reason, self._clock.now())
        except Exception:  # noqa: BLE001 - el respaldo nunca tumba nada: queda «fallido»
            invoice.record_archive(ArchiveStatus.FAILED, _UNEXPECTED, self._clock.now())
        else:
            invoice.record_archive(ArchiveStatus.DONE, "", self._clock.now())
        return await self._invoices.save(invoice)
