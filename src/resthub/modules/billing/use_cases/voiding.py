"""Anular un comprobante para emitir otro: boleta por factura, otro RUC o DNI.

Exige `billing.manage` (el encargado). Según cómo quedó ante SUNAT:

- Aceptado: se emite una nota de crédito de motivo 01 por el mismo total. El
  comprobante queda anulado solo cuando SUNAT acepta la nota; si queda pendiente
  o rechazada, se reenvía la nota (`ResendCreditNote`).
- Rechazado o sin enviar: nunca valió ante SUNAT, así que se anula solo en
  RestHub, con su motivo.
- Pendiente: no se anula hasta que se resuelva; antes se reenvía.

Con el comprobante anulado, `IssueInvoice` emite el nuevo para el mismo pedido.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.identity import Principal
from resthub.modules.billing.domain.credit_notes import CreditNote
from resthub.modules.billing.domain.exceptions import (
    CreditNoteNotFoundError,
    InvoiceNotFoundError,
)
from resthub.modules.billing.domain.invoices import Invoice, InvoiceStatus, clean_void_reason
from resthub.modules.billing.ports.billing_ports import (
    BillingSettingsRepository,
    CreditNoteRepository,
    ElectronicInvoiceGateway,
    InvoiceRepository,
    PaidOrderDirectory,
)
from resthub.modules.billing.ports.delivery_ports import DocumentFile
from resthub.modules.billing.use_cases.invoicing import (
    DocumentFormat,
    ReadInvoice,
    document_filename,
    find_invoice,
    invoice_document,
    unready_reason,
)


@dataclass(frozen=True, slots=True)
class VoidInvoiceCommand:
    actor: Principal
    invoice_id: int
    reason: str


class _NoteSender:
    """Envía una nota de crédito y, si SUNAT la acepta, anula el comprobante."""

    def __init__(
        self,
        invoices: InvoiceRepository,
        notes: CreditNoteRepository,
        settings: BillingSettingsRepository,
        invoicer: ElectronicInvoiceGateway,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._invoices = invoices
        self._notes = notes
        self._settings = settings
        self._invoicer = invoicer
        self._activity = activity
        self._clock = clock

    async def send(self, actor: Principal, note: CreditNote, invoice: Invoice) -> Invoice:
        config = await self._settings.get(invoice.restaurant_id)
        unready = unready_reason(config, self._clock.now())
        if unready:
            note.record_result(InvoiceStatus.SIMULATED, unready, {})
        else:
            result = await self._invoicer.send_note(config, note, invoice)
            note.record_result(
                result.status,
                result.message,
                result.raw or {},
                signed_xml=result.signed_xml,
                cdr_zip=result.cdr_zip,
                document_payload=result.document_payload,
            )
        note = await self._notes.save(note)
        await self._record(
            actor,
            ActivityKind.CREDIT_NOTE_ISSUED,
            f"Nota de crédito {note.code} de {invoice.kind.label.lower()} {invoice.code} "
            f"(S/ {note.total}): {note.status.label}",
        )
        invoice.credit_note = note
        if note.status is not InvoiceStatus.ACCEPTED:
            return invoice
        invoice.void(note.reason, actor.user_id, self._clock.now())
        saved = await self._invoices.save(invoice)
        await self._record(
            actor,
            ActivityKind.INVOICE_VOIDED,
            f"{invoice.kind.label} {invoice.code} anulada con la nota de crédito {note.code}: "
            f"{note.reason}",
        )
        return saved

    async def _record(self, actor: Principal, kind: ActivityKind, detail: str) -> None:
        await self._activity.record(actor.restaurant_id, actor.user_id, kind, detail)


class VoidInvoice:
    def __init__(
        self,
        invoices: InvoiceRepository,
        notes: CreditNoteRepository,
        settings: BillingSettingsRepository,
        invoicer: ElectronicInvoiceGateway,
        activity: ActivityRecorder,
        *,
        clock: Clock,
    ) -> None:
        self._invoices = invoices
        self._notes = notes
        self._settings = settings
        self._activity = activity
        self._clock = clock
        self._sender = _NoteSender(invoices, notes, settings, invoicer, activity, clock)

    async def __call__(self, command: VoidInvoiceCommand) -> Invoice:
        actor = command.actor
        reason = clean_void_reason(command.reason)
        # Tomado: dos anulaciones a la vez emitirían dos notas para el mismo comprobante.
        invoice = await find_invoice(
            self._invoices, actor.restaurant_id, command.invoice_id, for_update=True
        )
        invoice.ensure_voidable()
        if not invoice.needs_credit_note:
            status = invoice.status.label.lower()
            invoice.void(reason, actor.user_id, self._clock.now())
            saved = await self._invoices.save(invoice)
            await self._activity.record(
                actor.restaurant_id,
                actor.user_id,
                ActivityKind.INVOICE_VOIDED,
                f"{invoice.kind.label} {invoice.code} anulada sin nota de crédito ({status}): "
                f"{reason}",
            )
            return saved
        config = await self._settings.get(actor.restaurant_id)
        series = config.credit_series_for(invoice.kind)
        note = await self._notes.add(
            CreditNote(
                restaurant_id=actor.restaurant_id,
                invoice_id=invoice.id or 0,
                series=series,
                number=await self._notes.next_number(actor.restaurant_id, series),
                reason=reason,
                total=invoice.total,
                issued_by=actor.user_id,
                issued_at=self._clock.now(),
            )
        )
        return await self._sender.send(actor, note, invoice)


class ResendCreditNote:
    """Reenvía una nota pendiente, rechazada o sin enviar; aceptada, anula el comprobante."""

    def __init__(
        self,
        invoices: InvoiceRepository,
        notes: CreditNoteRepository,
        settings: BillingSettingsRepository,
        invoicer: ElectronicInvoiceGateway,
        activity: ActivityRecorder,
        *,
        clock: Clock,
    ) -> None:
        self._invoices = invoices
        self._notes = notes
        self._sender = _NoteSender(invoices, notes, settings, invoicer, activity, clock)

    async def __call__(self, actor: Principal, credit_note_id: int) -> Invoice:
        found = await self._notes.get(actor.restaurant_id, credit_note_id)
        if found is None:
            raise CreditNoteNotFoundError(credit_note_id)
        # El comprobante primero, como al anular: así dos pedidos nunca se toman al revés.
        invoice = await find_invoice(
            self._invoices, actor.restaurant_id, found.invoice_id, for_update=True
        )
        note = await self._notes.get(actor.restaurant_id, credit_note_id, for_update=True)
        if note is None:
            raise CreditNoteNotFoundError(credit_note_id)
        note.ensure_resendable()
        return await self._sender.send(actor, note, invoice)


class ReadCreditNoteDocument:
    """Descargar un documento de la nota exige poder ver el comprobante que anula."""

    def __init__(
        self,
        invoices: InvoiceRepository,
        notes: CreditNoteRepository,
        orders: PaidOrderDirectory,
        invoicer: ElectronicInvoiceGateway,
    ) -> None:
        self._reader = ReadInvoice(invoices, orders)
        self._notes = notes
        self._invoicer = invoicer

    async def __call__(
        self, actor: Principal, credit_note_id: int, document_format: DocumentFormat
    ) -> tuple[DocumentFile, str]:
        note = await self._notes.get(actor.restaurant_id, credit_note_id)
        if note is None:
            raise CreditNoteNotFoundError(credit_note_id)
        try:
            await self._reader.by_id(actor, note.invoice_id)
        except InvoiceNotFoundError as error:
            raise CreditNoteNotFoundError(credit_note_id) from error
        document = await invoice_document(note, document_format, self._invoicer)
        return document, document_filename(note, document_format)
