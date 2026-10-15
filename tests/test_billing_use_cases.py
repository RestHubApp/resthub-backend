"""Casos de uso de comprobantes con dobles en memoria.

Emitir, reenviar, configurar, subir el certificado y leer los documentos.

Cada caso afirma la serie y el correlativo que toma el comprobante, lo que se
le manda al servicio de facturación (o que no se le manda), el estado con que
queda y el asiento de bitácora.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.identity import Principal
from resthub.core.pagination import Page
from resthub.core.permissions import Permission
from resthub.modules.billing.domain.exceptions import (
    BillingProviderError,
    InvalidBillingSettingsError,
    InvalidCertificateError,
    InvalidInvoiceError,
    InvoiceDocumentUnavailableError,
    InvoiceNotFoundError,
    OrderAlreadyInvoicedError,
    PaidOrderNotFoundError,
)
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
)
from resthub.modules.billing.ports.billing_ports import (
    InvoiceQuery,
    PaidOrder,
    ParsedCertificate,
    ProviderResult,
)
from resthub.modules.billing.use_cases.invoicing import (
    DocumentFormat,
    IssueInvoice,
    IssueInvoiceCommand,
    ListInvoices,
    ReadInvoice,
    ReadInvoiceDocument,
    ResendInvoice,
    SettingsChange,
    UpdateBillingSettings,
    UploadCertificate,
    UploadCertificateCommand,
    can_see_paid_order,
    find_invoice,
    invoice_document,
)
from tests.conftest import RecordingActivity
from tests.fakes import FIXED_NOW, FixedClock

RESTAURANT = 1
OTHER_RESTAURANT = 2
D = Decimal
CERTIFICATE_PEM = "pem-de-prueba"
CASHIER = Principal(user_id=7, role_id=2, is_active=True, restaurant_id=RESTAURANT)
READY = BillingSettings(
    restaurant_id=RESTAURANT,
    ruc="20123456789",
    legal_name="Doña Rosa SAC",
    sol_user="MODDATOS",
    sol_password="moddatos",
    certificate_pem=CERTIFICATE_PEM,
    sales_receipt_series="B002",
    tax_invoice_series="F003",
    igv_rate=D("10.5"),
)
ORDER = PaidOrder(
    id=40,
    number=14,
    waiter_id=7,
    total=D("61.50"),
    discount=D("0.00"),
    items=(
        ("Lomo saltado", 2, D("28.00"), False),
        ("Chicha", 1, D("5.50"), False),
        ("Postre", 1, D("9"), True),
    ),
)


class InMemoryInvoices:
    def __init__(self) -> None:
        self.rows: dict[int, Invoice] = {}
        self.locks: list[int] = []
        self.saves = 0
        self.queries: list[InvoiceQuery] = []

    async def add(self, invoice: Invoice) -> Invoice:
        stored = replace(invoice, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return stored

    async def get(
        self, restaurant_id: int, invoice_id: int, *, for_update: bool = False
    ) -> Invoice | None:
        if for_update:
            self.locks.append(invoice_id)
        found = self.rows.get(invoice_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def for_order(self, restaurant_id: int, order_id: int) -> Invoice | None:
        return next(
            (
                i
                for i in self.rows.values()
                if i.restaurant_id == restaurant_id and i.order_id == order_id
            ),
            None,
        )

    async def save(self, invoice: Invoice) -> Invoice:
        self.saves += 1
        self.rows[invoice.id or 0] = invoice
        return invoice

    async def search(self, query: InvoiceQuery) -> Page[Invoice]:
        self.queries.append(query)
        return Page(items=list(self.rows.values()), total=len(self.rows))

    async def next_number(self, restaurant_id: int, series: str) -> int:
        return 1 + sum(1 for i in self.rows.values() if i.series == series)


class InMemorySettings:
    def __init__(self, config: BillingSettings) -> None:
        self.config = config
        self.saves: list[BillingSettings] = []
        self.requests: list[int] = []

    async def get(self, restaurant_id: int) -> BillingSettings:
        self.requests.append(restaurant_id)
        return self.config

    async def save(self, settings: BillingSettings) -> BillingSettings:
        self.saves.append(settings)
        self.config = settings
        return settings


class PaidOrders:
    def __init__(self, *orders: PaidOrder) -> None:
        self.orders = {p.id: p for p in orders}

    async def get(self, restaurant_id: int, order_id: int) -> PaidOrder | None:
        return self.orders.get(order_id) if restaurant_id == RESTAURANT else None

    async def waiter_of(self, restaurant_id: int, order_id: int) -> int | None:
        order = await self.get(restaurant_id, order_id)
        return None if order is None else order.waiter_id


class FakeProvider:
    """El servicio de facturación en memoria: anota lo que se le pide."""

    def __init__(
        self, result: ProviderResult | None = None, *, fail_register: bool = False
    ) -> None:
        self.result = result or ACCEPTED_RESULT
        self.fail_register = fail_register
        self.sends: list[tuple[BillingSettings, Invoice, InvoiceKind]] = []
        self.rendered: list[Invoice] = []
        self.registered: list[BillingSettings] = []
        self.unregistered: list[str] = []

    async def send(
        self, settings: BillingSettings, invoice: Invoice, kind: InvoiceKind
    ) -> ProviderResult:
        self.sends.append((settings, invoice, kind))
        return self.result

    async def render_pdf(self, invoice: Invoice) -> bytes:
        self.rendered.append(invoice)
        return b"%PDF-falso"

    async def register(self, settings: BillingSettings) -> None:
        if self.fail_register:
            message = "El servicio de facturación no aceptó las credenciales del local."
            raise BillingProviderError(message)
        self.registered.append(settings)

    async def unregister(self, ruc: str) -> None:
        self.unregistered.append(ruc)


ACCEPTED_RESULT = ProviderResult(
    InvoiceStatus.ACCEPTED,
    "Aceptada",
    {"ok": True},
    signed_xml="<Invoice/>",
    cdr_zip="UEs=",
    document_payload={"serie": "B002"},
)


def _issue(
    config: BillingSettings = READY, result: ProviderResult = ACCEPTED_RESULT
) -> tuple[IssueInvoice, InMemoryInvoices, FakeProvider, RecordingActivity]:
    invoices, provider, activity = InMemoryInvoices(), FakeProvider(result), RecordingActivity()
    issue = IssueInvoice(
        invoices,
        InMemorySettings(config),
        PaidOrders(ORDER),
        provider,
        activity,
        clock=FixedClock(),
    )
    return issue, invoices, provider, activity


async def test_sales_receipt_takes_the_restaurant_series_and_is_sent_to_sunat() -> None:
    issue, invoices, provider, activity = _issue()

    sales_receipt = await issue(
        IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer())
    )

    assert (sales_receipt.series, sales_receipt.number, sales_receipt.code) == ("B002", 1, "B002-1")
    assert (
        sales_receipt.order_id,
        sales_receipt.total,
        sales_receipt.discount,
        sales_receipt.igv_rate,
    ) == (
        40,
        D("61.50"),
        D("0.00"),
        D("10.50"),
    )
    assert (sales_receipt.taxable, sales_receipt.igv) == (D("55.66"), D("5.84"))
    # La cortesía va como transferencia gratuita: total cero.
    assert [(ln.description, ln.total, ln.is_courtesy) for ln in sales_receipt.lines] == [
        ("Lomo saltado", D("56.00"), False),
        ("Chicha", D("5.50"), False),
        ("Postre", D("0.00"), True),
    ]
    assert sales_receipt.customer.name == "Clientes varios"
    assert sales_receipt.issued_by == CASHIER.user_id
    assert (sales_receipt.status, sales_receipt.provider_response) == (
        InvoiceStatus.ACCEPTED,
        {"ok": True},
    )
    assert (
        sales_receipt.signed_xml,
        sales_receipt.cdr_zip,
        sales_receipt.document_payload,
    ) == ("<Invoice/>", "UEs=", {"serie": "B002"})
    assert (sales_receipt.has_xml, sales_receipt.has_cdr, sales_receipt.has_pdf) == (
        True,
        True,
        True,
    )
    ((sent_with, sent, kind),) = provider.sends
    assert (sent_with, sent.id, kind) == (READY, sales_receipt.id, InvoiceKind.SALES_RECEIPT)
    assert invoices.saves == 1
    assert activity.entries == [
        (
            RESTAURANT,
            7,
            ActivityKind.INVOICE_ISSUED,
            "Boleta de venta B002-1 del pedido #14 (S/ 61.50): Aceptado por SUNAT",
        )
    ]


async def test_tax_invoice_takes_its_own_series_and_sequence_number() -> None:
    issue, invoices, _, activity = _issue()
    customer = Customer(DocumentType.RUC, "20123456789", "Pollería SAC")
    invoices.rows[99] = replace(
        await invoices.add(
            Invoice(
                RESTAURANT,
                1,
                InvoiceKind.TAX_INVOICE,
                "F003",
                1,
                customer,
                (),
                D("1"),
                D("0"),
                D("18"),
                1,
            )
        ),
        id=99,
    )

    tax_invoice = await issue(IssueInvoiceCommand(CASHIER, 40, InvoiceKind.TAX_INVOICE, customer))

    assert tax_invoice.code == "F003-3"
    assert activity.entries[0][3].startswith("Factura F003-3 del pedido #14")


@pytest.mark.parametrize(
    "config",
    [
        BillingSettings(restaurant_id=RESTAURANT),
        replace(READY, certificate_pem=""),
        replace(READY, sol_password=""),
    ],
    ids=["nothing", "without-certificate", "without-sol-password"],
)
async def test_without_credentials_the_invoice_stays_unsent_and_no_call_is_made(
    config: BillingSettings,
) -> None:
    issue, _, provider, activity = _issue(config)

    sales_receipt = await issue(
        IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer())
    )

    assert sales_receipt.status is InvoiceStatus.SIMULATED
    assert sales_receipt.provider_message == (
        "Faltan el RUC, el usuario SOL o el certificado: se reenvía al cargarlos."
    )
    assert (sales_receipt.provider_response, sales_receipt.has_pdf) == ({}, False)
    assert sales_receipt.code == config.sales_receipt_series + "-1"
    assert provider.sends == []
    assert activity.entries[0][3].endswith(": Sin enviar (sin proveedor)")


async def test_response_without_raw_data_becomes_an_empty_dict() -> None:
    issue, _, _, _ = _issue(result=ProviderResult(InvoiceStatus.REJECTED, "RUC inválido"))

    sales_receipt = await issue(
        IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer())
    )

    assert (
        sales_receipt.status,
        sales_receipt.provider_message,
        sales_receipt.provider_response,
    ) == (
        InvoiceStatus.REJECTED,
        "RUC inválido",
        {},
    )


async def test_unpaid_is_not_issued_nor_twice_nor_with_an_invalid_customer() -> None:
    issue, invoices, provider, activity = _issue()
    first = await issue(IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer()))

    with pytest.raises(PaidOrderNotFoundError) as not_paid:
        await issue(IssueInvoiceCommand(CASHIER, 41, InvoiceKind.SALES_RECEIPT, Customer()))
    with pytest.raises(PaidOrderNotFoundError):
        await issue(
            IssueInvoiceCommand(
                replace(CASHIER, restaurant_id=OTHER_RESTAURANT),
                40,
                InvoiceKind.SALES_RECEIPT,
                Customer(),
            )
        )
    with pytest.raises(OrderAlreadyInvoicedError) as repeated:
        await issue(IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer()))

    assert not_paid.value.order_id == 41
    assert repeated.value.invoice_code == first.code
    assert len(invoices.rows) == 1
    assert len(provider.sends) == 1
    assert len(activity.entries) == 1


async def test_tax_invoice_without_ruc_takes_no_number() -> None:
    issue, invoices, _, _ = _issue()

    with pytest.raises(InvalidInvoiceError):
        await issue(IssueInvoiceCommand(CASHIER, 40, InvoiceKind.TAX_INVOICE, Customer()))
    assert invoices.rows == {}


async def test_resend_takes_the_invoice_and_sends_it_again() -> None:
    invoices = InMemoryInvoices()
    rejected = await invoices.add(
        Invoice(
            RESTAURANT,
            40,
            InvoiceKind.SALES_RECEIPT,
            "B002",
            1,
            Customer(),
            (),
            D("10"),
            D("0"),
            D("18"),
            7,
            status=InvoiceStatus.REJECTED,
        )
    )
    provider = FakeProvider(ACCEPTED_RESULT)
    resend = ResendInvoice(invoices, InMemorySettings(READY), provider, clock=FixedClock())

    resent = await resend(RESTAURANT, rejected.id or 0)

    assert resent.status is InvoiceStatus.ACCEPTED
    assert (resent.signed_xml, resent.document_payload) == ("<Invoice/>", {"serie": "B002"})
    assert invoices.locks == [rejected.id]
    assert len(provider.sends) == 1
    with pytest.raises(InvalidInvoiceError) as accepted:
        await resend(RESTAURANT, rejected.id or 0)
    with pytest.raises(InvoiceNotFoundError) as foreign:
        await resend(OTHER_RESTAURANT, rejected.id or 0)
    assert str(accepted.value) == "El comprobante B002-1 ya fue aceptado por SUNAT."
    assert foreign.value.invoice_id == rejected.id
    assert len(provider.sends) == 1


async def test_finding_an_invoice_does_not_lock_by_default() -> None:
    invoices = InMemoryInvoices()
    sales_receipt = await invoices.add(
        Invoice(
            RESTAURANT,
            40,
            InvoiceKind.SALES_RECEIPT,
            "B001",
            1,
            Customer(),
            (),
            D("1"),
            D("0"),
            D("18"),
            7,
        )
    )

    assert await find_invoice(invoices, RESTAURANT, sales_receipt.id or 0) is sales_receipt
    assert invoices.locks == []
    with pytest.raises(InvoiceNotFoundError):
        await find_invoice(invoices, OTHER_RESTAURANT, sales_receipt.id or 0)


async def test_listing_invoices_passes_the_query_as_is() -> None:
    invoices = InMemoryInvoices()
    query = InvoiceQuery(restaurant_id=RESTAURANT, status=InvoiceStatus.PENDING, limit=5)

    page = await ListInvoices(invoices)(query)

    assert (page.items, page.total) == ([], 0)
    assert invoices.queries == [query]


def _change(**data: object) -> SettingsChange:
    fields: dict[str, object] = {
        "ruc": "20123456789",
        "legal_name": "Rosa SAC",
        "address": "Jr. Pizarro 450",
        "igv_rate": D("18"),
        "sales_receipt_series": "B001",
        "tax_invoice_series": "F001",
    }
    fields.update(data)
    return SettingsChange(**fields)  # type: ignore[arg-type]


def _update(
    config: BillingSettings = READY, provider: FakeProvider | None = None
) -> tuple[UpdateBillingSettings, InMemorySettings, FakeProvider, RecordingActivity]:
    settings_store, activity = InMemorySettings(config), RecordingActivity()
    provider = provider or FakeProvider(ACCEPTED_RESULT)
    return (
        UpdateBillingSettings(settings_store, activity, provider),
        settings_store,
        provider,
        activity,
    )


async def test_configure_keeps_the_credentials_and_timezone_if_not_sent() -> None:
    update, _, provider, activity = _update(replace(READY, timezone="America/Bogota"))

    saved = await update(RESTAURANT, 9, _change())

    assert (saved.sol_user, saved.sol_password, saved.certificate_pem, saved.timezone) == (
        "MODDATOS",
        "moddatos",
        CERTIFICATE_PEM,
        "America/Bogota",
    )
    assert (saved.ruc, saved.legal_name, saved.address) == (
        "20123456789",
        "Rosa SAC",
        "Jr. Pizarro 450",
    )
    assert (saved.igv_rate, saved.sales_receipt_series, saved.tax_invoice_series) == (
        D("18.00"),
        "B001",
        "F001",
    )
    # Nada de lo que firma cambió: no se vuelve a registrar.
    assert (provider.registered, provider.unregistered) == ([], [])
    assert activity.entries == [
        (
            RESTAURANT,
            9,
            ActivityKind.BILLING_SETTINGS_UPDATED,
            "RUC 20123456789, IGV 18.00 %, series B001 y F001",
        )
    ]


async def test_new_sol_credentials_are_registered_and_logged_without_them() -> None:
    update, settings_store, provider, activity = _update()

    saved = await update(RESTAURANT, 9, _change(sol_user=" otrouser ", sol_password="clave-nueva"))

    assert (saved.sol_user, saved.sol_password) == ("OTROUSER", "clave-nueva")
    assert provider.registered == [saved]
    assert settings_store.saves == [saved]
    detail = activity.entries[0][3]
    assert (
        detail == "RUC 20123456789, IGV 18.00 %, series B001 y F001; credenciales SOL reemplazadas"
    )
    assert "OTROUSER" not in detail
    assert "clave-nueva" not in detail


async def test_sol_credentials_can_be_deleted() -> None:
    update, _, provider, activity = _update()

    deleted = await update(RESTAURANT, 9, _change(sol_user="", sol_password=""))

    assert (deleted.has_sol_credentials, deleted.is_ready(FIXED_NOW)) == (False, False)
    # Sin credenciales no hay qué registrar.
    assert provider.registered == []
    assert activity.entries[0][3].endswith("; credenciales SOL borradas")


async def test_sol_user_and_password_go_together() -> None:
    update, settings_store, provider, activity = _update()

    with pytest.raises(InvalidBillingSettingsError) as error:
        await update(RESTAURANT, 9, _change(sol_user="OTRO"))

    assert error.value.reason == "El usuario y la clave SOL se cambian juntos."
    assert (settings_store.saves, provider.registered, activity.entries) == ([], [], [])


async def test_changing_the_ruc_requires_retyping_the_sol_credentials() -> None:
    """Las credenciales son de un RUC: con otro no sirven."""
    update, settings_store, provider, activity = _update()

    with pytest.raises(InvalidBillingSettingsError) as error:
        await update(RESTAURANT, 9, _change(ruc="20999999991"))

    assert error.value.reason == (
        "Si cambias el RUC, vuelve a escribir el usuario y la clave SOL de ese RUC."
    )
    assert (settings_store.saves, provider.registered, activity.entries) == ([], [], [])


async def test_new_ruc_is_registered_and_the_old_one_forgotten() -> None:
    update, _, provider, _ = _update()

    saved = await update(
        RESTAURANT, 9, _change(ruc="20999999991", sol_user="NUEVO", sol_password="clave")
    )

    assert [s.ruc for s in provider.registered] == ["20999999991"]
    assert provider.unregistered == ["20123456789"]
    assert saved.ruc == "20999999991"


async def test_ruc_change_without_previous_credentials_asks_for_nothing() -> None:
    update, _, provider, _ = _update(BillingSettings(restaurant_id=RESTAURANT, ruc="20123456789"))

    saved = await update(RESTAURANT, 9, _change(ruc="20999999991"))

    assert (saved.ruc, saved.is_ready(FIXED_NOW)) == ("20999999991", False)
    # Nunca se registró: no hay nada que olvidar ni que registrar.
    assert (provider.registered, provider.unregistered) == ([], [])


async def test_if_the_billing_service_refuses_nothing_is_saved() -> None:
    update, settings_store, _, activity = _update(provider=FakeProvider(fail_register=True))

    with pytest.raises(BillingProviderError):
        await update(RESTAURANT, 9, _change(sol_user="OTRO", sol_password="x"))

    assert (settings_store.saves, activity.entries) == ([], [])


async def test_configure_takes_the_restaurant_fiscal_data_and_saves_everything_sent() -> None:
    update, settings_store, _, activity = _update(replace(READY, igv_rate=D("10.5")))

    saved = await update(
        RESTAURANT,
        9,
        _change(igv_rate=D("20.5"), sales_receipt_series="B007", tax_invoice_series="F008"),
    )

    assert settings_store.requests == [RESTAURANT]
    assert (saved.igv_rate, saved.sales_receipt_series, saved.tax_invoice_series) == (
        D("20.50"),
        "B007",
        "F008",
    )
    assert activity.entries == [
        (
            RESTAURANT,
            9,
            ActivityKind.BILLING_SETTINGS_UPDATED,
            "RUC 20123456789, IGV 20.50 %, series B007 y F008",
        )
    ]


# -- Certificado digital ---------------------------------------------------------


class FakeCertificateReader:
    def __init__(self, parsed: ParsedCertificate | None = None) -> None:
        self.parsed = parsed or ParsedCertificate(
            pem="pem-de-prueba-nuevo", expires_at=VALID_UNTIL, subject="ROSA SAC"
        )
        self.reads: list[tuple[bytes, str]] = []

    def read(self, content: bytes, password: str) -> ParsedCertificate:
        self.reads.append((content, password))
        if content == b"roto":
            message = "No se pudo abrir el certificado: revisa el archivo y su contraseña."
            raise InvalidCertificateError(message)
        return self.parsed


VALID_UNTIL = datetime(2030, 1, 1, tzinfo=UTC)


def _upload(
    config: BillingSettings = READY,
    reader: FakeCertificateReader | None = None,
    provider: FakeProvider | None = None,
) -> tuple[UploadCertificate, InMemorySettings, FakeProvider, RecordingActivity]:
    settings_store, activity = InMemorySettings(config), RecordingActivity()
    provider = provider or FakeProvider(ACCEPTED_RESULT)
    upload = UploadCertificate(
        settings_store, reader or FakeCertificateReader(), provider, activity, FixedClock()
    )
    return upload, settings_store, provider, activity


async def test_certificate_is_saved_registered_and_logged() -> None:
    reader = FakeCertificateReader()
    upload, settings_store, provider, activity = _upload(reader=reader)

    saved = await upload(UploadCertificateCommand(RESTAURANT, 9, b"pfx", "clave"))

    assert reader.reads == [(b"pfx", "clave")]
    assert (saved.certificate_pem, saved.certificate_expires_at) == (
        "pem-de-prueba-nuevo",
        VALID_UNTIL,
    )
    assert (saved.sol_user, saved.ruc) == ("MODDATOS", "20123456789")
    assert provider.registered == [saved]
    assert settings_store.saves == [saved]
    assert activity.entries == [
        (
            RESTAURANT,
            9,
            ActivityKind.BILLING_SETTINGS_UPDATED,
            "Certificado digital reemplazado (ROSA SAC, vence 2030-01-01)",
        )
    ]


async def test_certificate_without_sol_credentials_is_saved_but_not_registered() -> None:
    upload, _, provider, _ = _upload(replace(READY, sol_user="", sol_password=""))

    saved = await upload(UploadCertificateCommand(RESTAURANT, 9, b"pfx", "clave"))

    assert saved.has_certificate
    assert provider.registered == []


async def test_expired_or_broken_certificate_is_refused() -> None:
    expired = FakeCertificateReader(
        ParsedCertificate(pem="pem", expires_at=FIXED_NOW - timedelta(days=1), subject="X")
    )
    upload, settings_store, provider, activity = _upload(reader=expired)

    with pytest.raises(InvalidCertificateError) as error:
        await upload(UploadCertificateCommand(RESTAURANT, 9, b"pfx", "clave"))
    assert error.value.reason.startswith("El certificado venció el ")

    broken, store2, provider2, _ = _upload()
    with pytest.raises(InvalidCertificateError):
        await broken(UploadCertificateCommand(RESTAURANT, 9, b"roto", "mala"))

    assert (settings_store.saves, store2.saves) == ([], [])
    assert (provider.registered, provider2.registered, activity.entries) == ([], [], [])


async def test_certificate_without_expiry_is_logged_as_such() -> None:
    reader = FakeCertificateReader(ParsedCertificate(pem="pem", expires_at=None, subject=""))
    upload, _, _, activity = _upload(reader=reader)

    saved = await upload(UploadCertificateCommand(RESTAURANT, 9, b"pem", ""))

    assert saved.certificate_expires_at is None
    assert activity.entries[0][3] == "Certificado digital reemplazado (sin nombre, vence sin fecha)"


async def test_certificate_refused_by_the_billing_service_is_not_saved() -> None:
    upload, settings_store, _, activity = _upload(provider=FakeProvider(fail_register=True))

    with pytest.raises(BillingProviderError):
        await upload(UploadCertificateCommand(RESTAURANT, 9, b"pfx", "clave"))

    assert (settings_store.saves, activity.entries) == ([], [])


async def test_invoice_carries_its_issue_time_in_utc() -> None:
    issue, _, _, _ = _issue()

    invoice = await issue(IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer()))

    assert invoice.issued_at == FIXED_NOW
    assert invoice.issued_at.tzinfo is UTC


# -- Un mesero solo ve el comprobante de lo que tomó él --------------------------

OTHER_WAITER = replace(CASHIER, user_id=8)
OWNER = replace(CASHIER, user_id=9, permissions=frozenset({Permission.ORDERS_READ_ALL.value}))


def test_without_orders_read_all_only_the_own_order_is_visible() -> None:
    assert can_see_paid_order(CASHIER, 7)
    assert not can_see_paid_order(OTHER_WAITER, 7)
    assert can_see_paid_order(OWNER, 7)
    assert not can_see_paid_order(CASHIER, None)
    assert not can_see_paid_order(OWNER, None)


async def test_waiter_does_not_issue_the_invoice_of_a_foreign_order() -> None:
    issue, invoices, provider, activity = _issue()

    with pytest.raises(PaidOrderNotFoundError):
        await issue(IssueInvoiceCommand(OTHER_WAITER, 40, InvoiceKind.SALES_RECEIPT, Customer()))
    own = await issue(IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer()))

    assert own.order_id == 40
    assert len(invoices.rows) == 1
    assert len(provider.sends) == 1
    assert len(activity.entries) == 1


async def test_owner_issues_the_invoice_of_any_order() -> None:
    issue, _, _, _ = _issue()

    sales_receipt = await issue(
        IssueInvoiceCommand(OWNER, 40, InvoiceKind.SALES_RECEIPT, Customer())
    )

    assert sales_receipt.issued_by == OWNER.user_id


async def test_waiter_does_not_read_the_invoice_of_a_foreign_order() -> None:
    issue, invoices, _, _ = _issue()
    issued = await issue(IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer()))
    reader = ReadInvoice(invoices, PaidOrders(ORDER))

    assert (await reader.by_id(CASHIER, issued.id)).id == issued.id
    assert (await reader.by_id(OWNER, issued.id)).id == issued.id
    assert (await reader.for_order(CASHIER, 40)).id == issued.id  # type: ignore[union-attr]
    assert (await reader.for_order(OWNER, 40)).id == issued.id  # type: ignore[union-attr]
    with pytest.raises(InvoiceNotFoundError):
        await reader.by_id(OTHER_WAITER, issued.id)
    assert await reader.for_order(OTHER_WAITER, 40) is None


# -- Documentos: PDF, XML firmado y CDR -------------------------------------------


def _with_documents(**changes: object) -> Invoice:
    invoice = Invoice(
        RESTAURANT,
        40,
        InvoiceKind.SALES_RECEIPT,
        "B002",
        1,
        Customer(),
        (),
        D("10"),
        D("0"),
        D("18"),
        7,
        status=InvoiceStatus.ACCEPTED,
        signed_xml="<Invoice/>",
        cdr_zip=base64.b64encode(b"PK-cdr").decode(),
        document_payload={"serie": "B002"},
    )
    return replace(invoice, **changes)  # type: ignore[arg-type]


async def test_documents_come_from_the_invoice_and_the_pdf_from_the_service() -> None:
    invoice, provider = _with_documents(), FakeProvider(ACCEPTED_RESULT)

    xml = await invoice_document(invoice, DocumentFormat.XML, provider)
    cdr = await invoice_document(invoice, DocumentFormat.CDR, provider)
    pdf = await invoice_document(invoice, DocumentFormat.PDF, provider)

    assert (xml.content, xml.media_type) == (b"<Invoice/>", "application/xml")
    assert (cdr.content, cdr.media_type) == (b"PK-cdr", "application/zip")
    assert (pdf.content, pdf.media_type) == (b"%PDF-falso", "application/pdf")
    assert provider.rendered == [invoice]


@pytest.mark.parametrize(
    ("document_format", "changes", "message"),
    [
        (
            DocumentFormat.XML,
            {"signed_xml": ""},
            "El comprobante B002-1 todavía no tiene XML firmado.",
        ),
        (
            DocumentFormat.CDR,
            {"cdr_zip": ""},
            "El comprobante B002-1 todavía no tiene constancia de SUNAT.",
        ),
        (
            DocumentFormat.CDR,
            {"cdr_zip": "no es base64!"},
            "La constancia de B002-1 está dañada; reenvía el comprobante.",
        ),
        (
            DocumentFormat.PDF,
            {"document_payload": {}},
            "El comprobante B002-1 todavía no tiene PDF.",
        ),
    ],
)
async def test_missing_or_broken_documents_are_unavailable(
    document_format: DocumentFormat, changes: dict[str, object], message: str
) -> None:
    provider = FakeProvider(ACCEPTED_RESULT)

    with pytest.raises(InvoiceDocumentUnavailableError) as error:
        await invoice_document(_with_documents(**changes), document_format, provider)

    assert error.value.reason == message
    assert provider.rendered == []


async def test_reading_a_document_respects_who_can_see_the_invoice() -> None:
    invoices = InMemoryInvoices()
    stored = await invoices.add(_with_documents())
    read = ReadInvoiceDocument(invoices, PaidOrders(ORDER), FakeProvider(ACCEPTED_RESULT))

    document, filename = await read(CASHIER, stored.id or 0, DocumentFormat.CDR)
    _, pdf_name = await read(OWNER, stored.id or 0, DocumentFormat.PDF)

    assert (document.content, filename, pdf_name) == (b"PK-cdr", "R-B002-1.zip", "B002-1.pdf")
    with pytest.raises(InvoiceNotFoundError):
        await read(OTHER_WAITER, stored.id or 0, DocumentFormat.XML)
