"""Casos de uso de comprobantes con dobles en memoria: emitir, reenviar y configurar.

Cada caso afirma la serie y el correlativo que toma el comprobante, lo que se
le manda al proveedor (o que no se le manda), el estado con que queda y el
asiento de bitácora.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.identity import Principal
from resthub.core.pagination import Page
from resthub.core.permissions import Permission
from resthub.modules.billing.domain.exceptions import (
    InvalidBillingSettingsError,
    InvalidInvoiceError,
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
from resthub.modules.billing.ports.billing_ports import InvoiceQuery, PaidOrder, ProviderResult
from resthub.modules.billing.use_cases.invoicing import (
    IssueInvoice,
    IssueInvoiceCommand,
    ListInvoices,
    ReadInvoice,
    ResendInvoice,
    SettingsChange,
    UpdateBillingSettings,
    can_see_paid_order,
    find_invoice,
)
from tests.conftest import RecordingActivity

RESTAURANT = 1
OTHER_RESTAURANT = 2
D = Decimal
CASHIER = Principal(user_id=7, role_id=2, is_active=True, restaurant_id=RESTAURANT)
READY = BillingSettings(
    restaurant_id=RESTAURANT,
    ruc="20123456789",
    legal_name="Doña Rosa SAC",
    provider_url="https://api.nubefact.com/x",
    provider_token="t",
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
    def __init__(self, result: ProviderResult) -> None:
        self.result = result
        self.sends: list[tuple[BillingSettings, Invoice, InvoiceKind]] = []

    async def send(
        self, settings: BillingSettings, invoice: Invoice, kind: InvoiceKind
    ) -> ProviderResult:
        self.sends.append((settings, invoice, kind))
        return self.result


ACCEPTED_RESULT = ProviderResult(InvoiceStatus.ACCEPTED, "Aceptada", "https://pdf/1", {"ok": True})


def _issue(
    config: BillingSettings = READY, result: ProviderResult = ACCEPTED_RESULT
) -> tuple[IssueInvoice, InMemoryInvoices, FakeProvider, RecordingActivity]:
    invoices, provider, activity = InMemoryInvoices(), FakeProvider(result), RecordingActivity()
    issue = IssueInvoice(invoices, InMemorySettings(config), PaidOrders(ORDER), provider, activity)
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
    assert [ln.description for ln in sales_receipt.lines] == ["Lomo saltado", "Chicha"]
    assert sales_receipt.customer.name == "Clientes varios"
    assert sales_receipt.issued_by == CASHIER.user_id
    assert (sales_receipt.status, sales_receipt.pdf_url, sales_receipt.provider_response) == (
        InvoiceStatus.ACCEPTED,
        "https://pdf/1",
        {"ok": True},
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


async def test_without_a_provider_the_invoice_stays_unsent_and_no_call_is_made() -> None:
    issue, _, provider, activity = _issue(BillingSettings(restaurant_id=RESTAURANT))

    sales_receipt = await issue(
        IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer())
    )

    assert sales_receipt.status is InvoiceStatus.SIMULATED
    assert sales_receipt.provider_message == (
        "Falta configurar el RUC y el proveedor: se reenvía al cargarlos."
    )
    assert (sales_receipt.pdf_url, sales_receipt.provider_response) == ("", {})
    assert sales_receipt.code == "B001-1"
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
    resend = ResendInvoice(invoices, InMemorySettings(READY), provider)

    resent = await resend(RESTAURANT, rejected.id or 0)

    assert resent.status is InvoiceStatus.ACCEPTED
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
        "provider_url": " https://api.nubefact.com/x ",
    }
    fields.update(data)
    return SettingsChange(**fields)  # type: ignore[arg-type]


async def test_configure_keeps_the_token_and_timezone_if_not_sent() -> None:
    settings_store = InMemorySettings(
        replace(READY, provider_token="token-de-ejemplo", timezone="America/Bogota")
    )
    activity = RecordingActivity()

    saved = await UpdateBillingSettings(settings_store, activity)(RESTAURANT, 9, _change())

    assert (saved.provider_token, saved.timezone) == (
        "token-de-ejemplo",
        "America/Bogota",
    )
    assert saved.provider_url == "https://api.nubefact.com/x"
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
    assert activity.entries == [
        (
            RESTAURANT,
            9,
            ActivityKind.BILLING_SETTINGS_UPDATED,
            "RUC 20123456789, IGV 18.00 %, series B001 y F001",
        )
    ]


async def test_configure_replaces_or_deletes_the_token() -> None:
    settings_store, activity = InMemorySettings(READY), RecordingActivity()
    update = UpdateBillingSettings(settings_store, activity)

    new = await update(RESTAURANT, 9, _change(provider_token="otro"))
    assert new.provider_token == "otro"
    deleted = await update(RESTAURANT, 9, _change(provider_token="", ruc=""))
    assert deleted.provider_token == ""
    assert [detail for *_, detail in activity.entries] == [
        "RUC 20123456789, IGV 18.00 %, series B001 y F001; token del proveedor reemplazado",
        "RUC —, IGV 18.00 %, series B001 y F001; token del proveedor borrado",
    ]


async def test_changing_the_provider_url_requires_retyping_the_token() -> None:
    """El token viaja a esa URL: quien la cambia sin conocerlo se lo llevaría a su servidor."""
    settings_store, activity = InMemorySettings(READY), RecordingActivity()

    with pytest.raises(InvalidBillingSettingsError) as error:
        await UpdateBillingSettings(settings_store, activity)(
            RESTAURANT, 9, _change(provider_url="https://atacante.example/captura")
        )

    assert error.value.reason == "Si cambias la ruta del proveedor, vuelve a escribir su token."
    assert settings_store.saves == []
    assert activity.entries == []


async def test_new_url_with_its_token_is_logged_without_the_path_or_the_token() -> None:
    settings_store, activity = InMemorySettings(READY), RecordingActivity()

    saved = await UpdateBillingSettings(settings_store, activity)(
        RESTAURANT,
        9,
        _change(provider_url="https://API.Nubefact.com/api/v1/ruta-secreta", provider_token="n"),
    )

    assert (saved.provider_url, saved.provider_token) == (
        "https://API.Nubefact.com/api/v1/ruta-secreta",
        "n",
    )
    assert activity.entries[0][3] == (
        "RUC 20123456789, IGV 18.00 %, series B001 y F001; token del proveedor reemplazado; "
        "proveedor en api.nubefact.com"
    )


@pytest.mark.parametrize(
    "url",
    ["https://api.nubefact.com/x", "HTTPS://API.NUBEFACT.COM/x", " https://api.nubefact.com./x "],
)
async def test_same_url_written_differently_keeps_the_token(url: str) -> None:
    settings_store, activity = InMemorySettings(READY), RecordingActivity()

    saved = await UpdateBillingSettings(settings_store, activity)(
        RESTAURANT, 9, _change(provider_url=url)
    )

    assert saved.provider_token == "t"
    assert activity.entries[0][3] == "RUC 20123456789, IGV 18.00 %, series B001 y F001"


@pytest.mark.parametrize("other", ["https://api.nubefact.com/y", "https://api.nubefact.com:8443/x"])
async def test_other_path_or_port_on_the_same_server_also_requires_the_token(
    other: str,
) -> None:
    with pytest.raises(InvalidBillingSettingsError):
        await UpdateBillingSettings(InMemorySettings(READY), RecordingActivity())(
            RESTAURANT, 9, _change(provider_url=other)
        )


async def test_removing_the_url_does_not_ask_for_the_token_and_is_logged() -> None:
    settings_store, activity = InMemorySettings(READY), RecordingActivity()

    saved = await UpdateBillingSettings(settings_store, activity)(
        RESTAURANT, 9, _change(provider_url="")
    )

    assert (saved.provider_url, saved.is_ready) == ("", False)
    assert activity.entries[0][3].endswith("; sin proveedor")


async def test_first_url_without_a_saved_token_asks_for_nothing() -> None:
    settings_store, activity = (
        InMemorySettings(BillingSettings(restaurant_id=RESTAURANT)),
        RecordingActivity(),
    )

    saved = await UpdateBillingSettings(settings_store, activity)(RESTAURANT, 9, _change())

    assert (saved.provider_url, saved.provider_token) == ("https://api.nubefact.com/x", "")
    assert activity.entries[0][3].endswith("; proveedor en api.nubefact.com")


async def test_insecure_url_is_not_saved() -> None:
    settings_store, activity = InMemorySettings(READY), RecordingActivity()

    with pytest.raises(InvalidBillingSettingsError):
        await UpdateBillingSettings(settings_store, activity)(
            RESTAURANT, 9, _change(provider_url="https://localhost/x")
        )
    assert settings_store.saves == []
    assert activity.entries == []


async def test_configure_takes_the_restaurant_fiscal_data_and_saves_everything_sent() -> None:
    settings_store, activity = (
        InMemorySettings(replace(READY, igv_rate=D("10.5"))),
        RecordingActivity(),
    )

    saved = await UpdateBillingSettings(settings_store, activity)(
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


async def test_invoice_carries_its_issue_time_in_utc() -> None:
    issue, _, _, _ = _issue()
    before = datetime.now(UTC)

    invoice = await issue(IssueInvoiceCommand(CASHIER, 40, InvoiceKind.SALES_RECEIPT, Customer()))

    assert invoice.issued_at >= before
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
