"""Anular comprobantes y reemitirlos, con dobles en memoria.

Cada caso afirma qué se le manda al servicio de facturación (la nota de crédito
o nada), si el comprobante queda anulado y el asiento de bitácora.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.modules.billing.domain.credit_notes import CreditNote, CreditNoteReason
from resthub.modules.billing.domain.exceptions import (
    CreditNoteNotFoundError,
    InvalidBillingSettingsError,
    InvalidInvoiceError,
    InvoiceAlreadyVoidedError,
    InvoiceDocumentUnavailableError,
    InvoiceNotFoundError,
    InvoiceNotVoidableError,
    OrderAlreadyInvoicedError,
)
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
)
from resthub.modules.billing.ports.billing_ports import ProviderResult
from resthub.modules.billing.use_cases.invoicing import (
    DocumentFormat,
    IssueInvoiceCommand,
    ResendInvoice,
    document_filename,
)
from resthub.modules.billing.use_cases.voiding import (
    ReadCreditNoteDocument,
    ResendCreditNote,
    VoidInvoice,
    VoidInvoiceCommand,
)
from tests.conftest import RecordingActivity
from tests.fakes import FIXED_NOW, FixedClock
from tests.test_billing_use_cases import (
    ACCEPTED_RESULT,
    CASHIER,
    ORDER,
    READY,
    RESTAURANT,
    FakeProvider,
    InMemoryInvoices,
    InMemorySettings,
    PaidOrders,
    _issue,
)

MANAGER = Principal(
    user_id=9,
    role_id=1,
    is_active=True,
    restaurant_id=RESTAURANT,
    permissions=frozenset({Permission.BILLING_MANAGE, Permission.ORDERS_READ_ALL}),
)
REASON = "El cliente pidió factura"
NOTE_ACCEPTED = ProviderResult(
    InvoiceStatus.ACCEPTED,
    "La Nota de Credito numero BC01-1, ha sido aceptada",
    {"ok": True},
    signed_xml="<CreditNote/>",
    cdr_zip="UEs=",
    document_payload={"serie": "BC01"},
)
NOTE_REJECTED = ProviderResult(InvoiceStatus.REJECTED, "Rechazado por SUNAT (2116): …", {})
NOTE_PENDING = ProviderResult(InvoiceStatus.PENDING, "SUNAT no respondió", {})


class InMemoryCreditNotes:
    def __init__(self) -> None:
        self.rows: dict[int, CreditNote] = {}
        self.locks: list[int] = []

    async def add(self, note: CreditNote) -> CreditNote:
        stored = replace(note, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return stored

    async def get(
        self, restaurant_id: int, credit_note_id: int, *, for_update: bool = False
    ) -> CreditNote | None:
        if for_update:
            self.locks.append(credit_note_id)
        found = self.rows.get(credit_note_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def save(self, note: CreditNote) -> CreditNote:
        self.rows[note.id or 0] = note
        return note

    async def next_number(self, restaurant_id: int, series: str) -> int:
        return 1 + sum(
            1 for n in self.rows.values() if n.series == series and n.restaurant_id == restaurant_id
        )


class Setup:
    """Un comprobante emitido y todo lo que hace falta para anularlo."""

    def __init__(
        self,
        *,
        status: InvoiceStatus = InvoiceStatus.ACCEPTED,
        kind: InvoiceKind = InvoiceKind.SALES_RECEIPT,
        config: BillingSettings = READY,
    ) -> None:
        self.invoices = InMemoryInvoices()
        self.notes = InMemoryCreditNotes()
        self.settings = InMemorySettings(config)
        self.provider = FakeProvider(ACCEPTED_RESULT)
        self.provider.note_result = NOTE_ACCEPTED
        self.activity = RecordingActivity()
        self.clock = FixedClock()
        customer = (
            Customer(DocumentType.RUC, "20100066603", "Cliente SAC")
            if kind is InvoiceKind.TAX_INVOICE
            else Customer()
        )
        self.invoice = Invoice(
            id=1,
            restaurant_id=RESTAURANT,
            order_id=ORDER.id,
            kind=kind,
            series=config.series_for(kind),
            number=7,
            customer=customer,
            lines=(),
            total=ORDER.total,
            discount=ORDER.discount,
            igv_rate=config.igv_rate,
            issued_by=CASHIER.user_id,
            status=status,
        )
        self.invoices.rows[1] = self.invoice

    def void(self) -> VoidInvoice:
        return VoidInvoice(
            self.invoices,
            self.notes,
            self.settings,
            self.provider,
            self.activity,
            clock=self.clock,
        )

    def resend(self) -> ResendCreditNote:
        return ResendCreditNote(
            self.invoices,
            self.notes,
            self.settings,
            self.provider,
            self.activity,
            clock=self.clock,
        )

    async def run(self, reason: str = REASON) -> Invoice:
        return await self.void()(VoidInvoiceCommand(MANAGER, 1, reason))


# -- Anular según el estado del comprobante ----------------------------------------


@pytest.mark.parametrize(
    ("kind", "series"),
    [(InvoiceKind.SALES_RECEIPT, "BC01"), (InvoiceKind.TAX_INVOICE, "FC01")],
    ids=["boleta", "factura"],
)
async def test_accepted_invoice_is_voided_by_an_accepted_credit_note(
    kind: InvoiceKind, series: str
) -> None:
    setup = Setup(kind=kind)

    voided = await setup.run("  El cliente   pidió factura ")

    note, sent_for = setup.provider.note_sends[0]
    assert (note.series, note.number, note.code) == (series, 1, f"{series}-1")
    assert (note.reason_code, note.reason, note.total) == (
        CreditNoteReason.VOID,
        REASON,
        ORDER.total,
    )
    assert sent_for.code == setup.invoice.code
    assert (voided.voided_at, voided.void_reason, voided.voided_by) == (
        FIXED_NOW,
        REASON,
        MANAGER.user_id,
    )
    assert voided.credit_note is not None
    assert voided.credit_note.status is InvoiceStatus.ACCEPTED
    assert voided.credit_note.signed_xml == "<CreditNote/>"
    assert setup.notes.rows[1].status is InvoiceStatus.ACCEPTED
    assert setup.invoices.locks == [1]
    assert setup.activity.kinds() == [ActivityKind.CREDIT_NOTE_ISSUED, ActivityKind.INVOICE_VOIDED]
    assert setup.activity.entries[0][3] == (
        f"Nota de crédito {series}-1 de {kind.label.lower()} {setup.invoice.code} "
        "(S/ 61.50): Aceptado por SUNAT"
    )
    assert setup.activity.entries[1][3] == (
        f"{kind.label} {setup.invoice.code} anulada con la nota de crédito {series}-1: {REASON}"
    )


@pytest.mark.parametrize("result", [NOTE_REJECTED, NOTE_PENDING], ids=["rejected", "pending"])
async def test_a_note_not_accepted_leaves_the_invoice_current(result: ProviderResult) -> None:
    setup = Setup()
    setup.provider.note_result = result

    invoice = await setup.run()

    assert not invoice.is_voided
    assert invoice.credit_note is not None
    assert invoice.credit_note.status is result.status
    assert setup.activity.kinds() == [ActivityKind.CREDIT_NOTE_ISSUED]
    # Mientras la nota esté en curso no se emite otra: se reenvía esa.
    with pytest.raises(InvoiceNotVoidableError, match="BC01-1"):
        await setup.run()
    assert len(setup.notes.rows) == 1


async def test_resending_the_note_until_accepted_voids_the_invoice() -> None:
    setup = Setup()
    setup.provider.note_result = NOTE_PENDING
    await setup.run()
    setup.provider.note_result = NOTE_ACCEPTED

    voided = await setup.resend()(MANAGER, 1)

    assert voided.is_voided
    assert voided.void_reason == REASON
    assert voided.credit_note is not None
    assert voided.credit_note.code == "BC01-1"
    assert len(setup.provider.note_sends) == 2
    assert setup.invoices.locks == [1, 1]
    assert setup.notes.locks == [1]
    assert setup.activity.kinds()[-1] is ActivityKind.INVOICE_VOIDED
    with pytest.raises(InvalidInvoiceError, match="ya fue aceptada"):
        await setup.resend()(MANAGER, 1)


async def test_resending_a_missing_note_is_not_found() -> None:
    with pytest.raises(CreditNoteNotFoundError):
        await Setup().resend()(MANAGER, 99)


@pytest.mark.parametrize("status", [InvoiceStatus.SIMULATED, InvoiceStatus.REJECTED])
async def test_an_invoice_that_never_counted_for_sunat_is_voided_without_a_note(
    status: InvoiceStatus,
) -> None:
    setup = Setup(status=status)

    voided = await setup.run()

    assert voided.is_voided
    assert voided.credit_note is None
    assert setup.provider.note_sends == []
    assert setup.notes.rows == {}
    assert setup.activity.kinds() == [ActivityKind.INVOICE_VOIDED]
    assert setup.activity.entries[0][3] == (
        f"Boleta de venta B002-7 anulada sin nota de crédito ({status.label.lower()}): {REASON}"
    )


async def test_a_pending_invoice_is_not_voided_until_resolved() -> None:
    setup = Setup(status=InvoiceStatus.PENDING)

    with pytest.raises(InvoiceNotVoidableError, match="pendiente"):
        await setup.run()

    assert setup.provider.note_sends == []
    assert not setup.invoice.is_voided


async def test_voiding_twice_is_a_conflict() -> None:
    setup = Setup()
    await setup.run()

    with pytest.raises(InvoiceAlreadyVoidedError, match="B002-7 ya está anulado"):
        await setup.run()

    assert len(setup.provider.note_sends) == 1


@pytest.mark.parametrize("reason", ["", "  ", "no", "x" * 201])
async def test_the_reason_is_required(reason: str) -> None:
    setup = Setup()

    with pytest.raises(InvalidInvoiceError, match="motivo"):
        await setup.run(reason)

    assert setup.invoices.locks == []


async def test_another_restaurants_invoice_is_not_found() -> None:
    setup = Setup()
    stranger = replace(MANAGER, restaurant_id=RESTAURANT + 1)

    with pytest.raises(InvoiceNotFoundError):
        await setup.void()(VoidInvoiceCommand(stranger, 1, REASON))


async def test_without_credentials_the_note_is_simulated_and_the_invoice_stays() -> None:
    setup = Setup(config=replace(READY, sol_password=""))

    invoice = await setup.run()

    assert setup.provider.note_sends == []
    assert invoice.credit_note is not None
    assert invoice.credit_note.status is InvoiceStatus.SIMULATED
    assert "Faltan el RUC" in invoice.credit_note.provider_message
    assert not invoice.is_voided


async def test_with_an_expired_certificate_the_note_is_simulated() -> None:
    expired = replace(READY, certificate_expires_at=FIXED_NOW - timedelta(days=1))
    setup = Setup(config=expired)

    invoice = await setup.run()

    assert invoice.credit_note is not None
    assert "venció" in invoice.credit_note.provider_message
    assert setup.provider.note_sends == []


async def test_a_voided_invoice_is_not_resent() -> None:
    setup = Setup(status=InvoiceStatus.REJECTED)
    await setup.run()
    resend = ResendInvoice(setup.invoices, setup.settings, setup.provider, clock=setup.clock)

    with pytest.raises(InvalidInvoiceError, match="anulado"):
        await resend(RESTAURANT, 1)


# -- Reemitir --------------------------------------------------------------------


async def test_after_voiding_the_order_takes_a_new_invoice_that_replaces_it() -> None:
    issue, invoices, _, activity = _issue()
    first = await issue(
        IssueInvoiceCommand(CASHIER, ORDER.id, InvoiceKind.SALES_RECEIPT, Customer())
    )
    customer = Customer(DocumentType.RUC, "20100066603", "Cliente SAC")
    with pytest.raises(OrderAlreadyInvoicedError, match="B002-1"):
        await issue(IssueInvoiceCommand(CASHIER, ORDER.id, InvoiceKind.TAX_INVOICE, customer))
    first.void(REASON, MANAGER.user_id, FIXED_NOW)

    second = await issue(IssueInvoiceCommand(CASHIER, ORDER.id, InvoiceKind.TAX_INVOICE, customer))

    assert (second.code, second.replaces_invoice_id) == ("F003-1", first.id)
    assert await invoices.for_order(RESTAURANT, ORDER.id) == second
    assert activity.entries[-1][3].endswith("; reemplaza a B002-1, anulado")


# -- Documentos de la nota -------------------------------------------------------


async def test_credit_note_documents_follow_the_invoice_visibility() -> None:
    setup = Setup()
    await setup.run()
    read = ReadCreditNoteDocument(setup.invoices, setup.notes, PaidOrders(ORDER), setup.provider)

    pdf, pdf_name = await read(MANAGER, 1, DocumentFormat.PDF)
    xml, xml_name = await read(CASHIER, 1, DocumentFormat.XML)
    cdr, cdr_name = await read(MANAGER, 1, DocumentFormat.CDR)

    assert (pdf.content, pdf_name) == (b"%PDF-nota", "BC01-1.pdf")
    assert (xml.content, xml_name) == (b"<CreditNote/>", "BC01-1.xml")
    assert (cdr.content, cdr_name) == (b"PK", "R-BC01-1.zip")
    other_waiter = replace(CASHIER, user_id=CASHIER.user_id + 1)
    with pytest.raises(CreditNoteNotFoundError):
        await read(other_waiter, 1, DocumentFormat.XML)
    with pytest.raises(CreditNoteNotFoundError):
        await read(MANAGER, 99, DocumentFormat.XML)


async def test_a_rejected_note_has_no_pdf() -> None:
    setup = Setup()
    setup.provider.note_result = NOTE_REJECTED
    await setup.run()
    read = ReadCreditNoteDocument(setup.invoices, setup.notes, PaidOrders(ORDER), setup.provider)

    with pytest.raises(InvoiceDocumentUnavailableError, match="La nota de crédito BC01-1"):
        await read(MANAGER, 1, DocumentFormat.PDF)
    with pytest.raises(InvoiceDocumentUnavailableError, match="XML"):
        await read(MANAGER, 1, DocumentFormat.XML)


async def test_damaged_note_cdr_asks_to_resend_the_note() -> None:
    setup = Setup()
    await setup.run()
    setup.notes.rows[1].cdr_zip = "no es base64!"
    read = ReadCreditNoteDocument(setup.invoices, setup.notes, PaidOrders(ORDER), setup.provider)

    with pytest.raises(InvoiceDocumentUnavailableError, match="reenvía la nota"):
        await read(MANAGER, 1, DocumentFormat.CDR)


def test_note_filenames_follow_sunat() -> None:
    note = CreditNote(
        restaurant_id=1,
        invoice_id=1,
        series="FC01",
        number=3,
        reason=REASON,
        total=ORDER.total,
        issued_by=1,
    )
    assert document_filename(note, DocumentFormat.CDR) == "R-FC01-3.zip"


# -- Series de las notas en los datos fiscales ---------------------------------------


def test_credit_note_series_default_to_bc01_and_fc01() -> None:
    settings = BillingSettings(restaurant_id=1)

    assert settings.credit_series_for(InvoiceKind.SALES_RECEIPT) == "BC01"
    assert settings.credit_series_for(InvoiceKind.TAX_INVOICE) == "FC01"
    custom = BillingSettings(
        restaurant_id=1, sales_receipt_credit_series="bn02", tax_invoice_credit_series=" fn03 "
    )
    assert (custom.sales_receipt_credit_series, custom.tax_invoice_credit_series) == (
        "BN02",
        "FN03",
    )


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"sales_receipt_credit_series": "FC01"}, "notas de crédito de boleta de venta"),
        ({"tax_invoice_credit_series": "BC01"}, "notas de crédito de factura"),
        ({"sales_receipt_credit_series": "BC1"}, "4 caracteres"),
        ({"sales_receipt_credit_series": "B001"}, "no puede ser la de los comprobantes"),
        ({"tax_invoice_credit_series": "F001"}, "no puede ser la de los comprobantes"),
    ],
)
def test_credit_note_series_rules(data: dict[str, str], message: str) -> None:
    with pytest.raises(InvalidBillingSettingsError, match=message):
        BillingSettings(restaurant_id=1, **data)
