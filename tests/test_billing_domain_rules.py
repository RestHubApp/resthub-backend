"""Límites y mensajes del dominio de comprobantes: datos fiscales, IGV, cliente y URL del proveedor.

Complementa `test_billing.py`: acá se fija cada mensaje de SUNAT que ve el
encargado, el redondeo del IGV al medio céntimo y cada forma de URL rechazada
con su motivo.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from resthub.core.clock import SystemClock
from resthub.modules.billing.domain.exceptions import (
    InvalidBillingSettingsError,
    InvalidInvoiceError,
    InvoiceNotFoundError,
    InvoiceNumberTakenError,
    OrderAlreadyInvoicedError,
    PaidOrderNotFoundError,
)
from resthub.modules.billing.domain.invoices import (
    ANONYMOUS_LIMIT,
    DEFAULT_IGV_RATE,
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    build_lines,
    provider_url_problem,
    split_igv,
    validate_customer,
    validate_provider_url,
)

D = Decimal


def _tax_invoice(**data: object) -> Invoice:
    fields: dict[str, object] = {
        "restaurant_id": 1,
        "order_id": 9,
        "kind": InvoiceKind.TAX_INVOICE,
        "series": "F001",
        "number": 15,
        "customer": Customer(DocumentType.RUC, "20123456789", "Pollería SAC"),
        "lines": (),
        "total": D("118.00"),
        "discount": D("0.00"),
        "igv_rate": D("18.00"),
        "issued_by": 1,
    }
    fields.update(data)
    return Invoice(**fields)  # type: ignore[arg-type]


# -- Rótulos y códigos ---------------------------------------------------------------


def test_sunat_labels_and_codes() -> None:
    assert [k.label for k in InvoiceKind] == ["Boleta de venta", "Factura"]
    assert [d.sunat_code for d in DocumentType] == ["-", "1", "4", "6"]
    assert [s.label for s in InvoiceStatus] == [
        "Aceptado por SUNAT",
        "Pendiente de envío",
        "Rechazado",
        "Sin enviar (sin proveedor)",
    ]


# -- Datos fiscales del local ------------------------------------------------------------


def test_default_fiscal_data_is_not_ready_to_issue() -> None:
    settings = BillingSettings(restaurant_id=1)

    assert (settings.igv_rate, settings.sales_receipt_series, settings.tax_invoice_series) == (
        DEFAULT_IGV_RATE,
        "B001",
        "F001",
    )
    assert not settings.is_ready
    assert settings.series_for(InvoiceKind.SALES_RECEIPT) == "B001"
    assert settings.series_for(InvoiceKind.TAX_INVOICE) == "F001"


def test_fiscal_data_is_normalized() -> None:
    settings = BillingSettings(
        restaurant_id=1,
        ruc=" 20123456789 ",
        legal_name="  Doña   Rosa  SAC ",
        address=" Jr.  Pizarro 450 ",
        igv_rate=D("10.5"),
        sales_receipt_series=" b002 ",
        tax_invoice_series="f00a",
        provider_url="  https://api.nubefact.com/x  ",
        provider_token="t",
    )

    assert settings.ruc == "20123456789"
    assert settings.legal_name == "Doña Rosa SAC"
    assert settings.address == "Jr. Pizarro 450"
    assert str(settings.igv_rate) == "10.50"
    assert (settings.sales_receipt_series, settings.tax_invoice_series) == ("B002", "F00A")
    assert settings.provider_url == "https://api.nubefact.com/x"
    assert settings.is_ready


@pytest.mark.parametrize("missing", ["ruc", "legal_name", "provider_url", "provider_token"])
def test_missing_any_of_the_four_fields_means_not_really_ready_to_issue(missing: str) -> None:
    data = {
        "ruc": "20123456789",
        "legal_name": "Rosa SAC",
        "provider_url": "https://api.nubefact.com/x",
        "provider_token": "t",
    }
    data[missing] = ""

    assert not BillingSettings(restaurant_id=1, **data).is_ready  # type: ignore[arg-type]


def test_legal_name_and_address_are_trimmed_to_the_maximum() -> None:
    settings = BillingSettings(restaurant_id=1, legal_name="L" * 150, address="A" * 250)

    assert (len(settings.legal_name), len(settings.address)) == (100, 200)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"ruc": "30123456789"}, "El RUC del local tiene 11 dígitos y empieza en 10 o 20."),
        ({"ruc": "2012345678"}, "El RUC del local tiene 11 dígitos y empieza en 10 o 20."),
        ({"igv_rate": D("-0.01")}, "La tasa de IGV va de 0 a 30 % con dos decimales."),
        ({"igv_rate": D("30.01")}, "La tasa de IGV va de 0 a 30 % con dos decimales."),
        ({"igv_rate": D("18.005")}, "La tasa de IGV va de 0 a 30 % con dos decimales."),
        (
            {"sales_receipt_series": "F001"},
            "La serie de boleta de venta son 4 caracteres y empieza con B.",
        ),
        (
            {"sales_receipt_series": "B0001"},
            "La serie de boleta de venta son 4 caracteres y empieza con B.",
        ),
        ({"tax_invoice_series": "B001"}, "La serie de factura son 4 caracteres y empieza con F."),
        ({"tax_invoice_series": "F01"}, "La serie de factura son 4 caracteres y empieza con F."),
    ],
)
def test_invalid_fiscal_data_explains_the_reason(data: dict[str, object], message: str) -> None:
    with pytest.raises(InvalidBillingSettingsError) as error:
        BillingSettings(restaurant_id=1, **data)  # type: ignore[arg-type]
    assert error.value.reason == message
    assert str(error.value) == message


@pytest.mark.parametrize("ruc", ["10123456789", "15123456789", "17123456789", "20123456789"])
def test_valid_ruc_prefixes(ruc: str) -> None:
    assert BillingSettings(restaurant_id=1, ruc=ruc).ruc == ruc


def test_boundary_igv_rates_are_accepted() -> None:
    assert BillingSettings(restaurant_id=1, igv_rate=D("0")).igv_rate == D("0.00")
    assert BillingSettings(restaurant_id=1, igv_rate=D("30")).igv_rate == D("30.00")


# -- IGV --------------------------------------------------------------------------------


def test_igv_rounds_the_base_to_the_half_cent_upwards() -> None:
    # 1.77 / 1.18 = 1.5 exacto; 0.59 / 1.18 = 0.5 exacto; 1.00 / 1.18 = 0.847…
    assert split_igv(D("1.77"), D("18")) == (D("1.50"), D("0.27"))
    assert split_igv(D("1.00"), D("18")) == (D("0.85"), D("0.15"))
    # 0.05 / 1.18 = 0.04237… y 0.03 / 1.18 = 0.02542…: se redondea, no se trunca.
    assert split_igv(D("0.03"), D("18")) == (D("0.03"), D("0.00"))
    assert split_igv(D("100.00"), D("0")) == (D("100.00"), D("0.00"))


def test_exact_half_cent_tie_rounds_upwards() -> None:
    # 3.13 con una tasa de 0.16 % da 3.125 exacto: la base es 3.13 y no 3.12.
    assert split_igv(D("3.13"), D("0.16")) == (D("3.13"), D("0.00"))


def test_base_and_igv_always_add_up_to_the_total() -> None:
    for cents in range(1, 400, 7):
        total = D(cents) / 100
        base, igv = split_igv(total, D("18"))
        assert base + igv == total


def test_invoice_computes_its_base_igv_and_code() -> None:
    invoice = _tax_invoice()

    assert (invoice.taxable, invoice.igv, invoice.code) == (D("100.00"), D("18.00"), "F001-15")


# -- Cliente ----------------------------------------------------------------------------


def test_tax_invoice_with_ruc_and_legal_name_is_normalized() -> None:
    customer = validate_customer(
        InvoiceKind.TAX_INVOICE,
        Customer(DocumentType.RUC, " 20123456789 ", "  Pollería   SAC ", " Av.  Grau 1 "),
        D("50"),
    )

    assert customer == Customer(DocumentType.RUC, "20123456789", "Pollería SAC", "Av. Grau 1")


@pytest.mark.parametrize(
    ("kind", "customer", "total", "message"),
    [
        (
            InvoiceKind.TAX_INVOICE,
            Customer(DocumentType.RUC, "2012345678", "SAC"),
            "10",
            "Una factura necesita el RUC del cliente (11 dígitos).",
        ),
        (
            InvoiceKind.TAX_INVOICE,
            Customer(DocumentType.DNI, "12345678", "Ana"),
            "10",
            "Una factura necesita el RUC del cliente (11 dígitos).",
        ),
        (
            InvoiceKind.TAX_INVOICE,
            Customer(DocumentType.RUC, "20123456789", "   "),
            "10",
            "Una factura necesita la razón social del cliente.",
        ),
        (
            InvoiceKind.SALES_RECEIPT,
            Customer(),
            "700.01",
            "Una boleta de más de S/ 700.00 necesita el documento del cliente.",
        ),
        (
            InvoiceKind.SALES_RECEIPT,
            Customer(DocumentType.DNI, "1234567", "Ana"),
            "10",
            "El número de DNI no es válido.",
        ),
        (
            InvoiceKind.SALES_RECEIPT,
            Customer(DocumentType.CE, "ABC-123", "Ana"),
            "10",
            "El número de CE no es válido.",
        ),
        (
            InvoiceKind.SALES_RECEIPT,
            Customer(DocumentType.RUC, "99123456789", "Ana"),
            "10",
            "El número de RUC no es válido.",
        ),
        (
            InvoiceKind.SALES_RECEIPT,
            Customer(DocumentType.DNI, "12345678", " "),
            "10",
            "Escribe el nombre del cliente.",
        ),
    ],
)
def test_each_sunat_rule_about_the_customer_has_its_message(
    kind: InvoiceKind, customer: Customer, total: str, message: str
) -> None:
    with pytest.raises(InvalidInvoiceError) as error:
        validate_customer(kind, customer, D(total))
    assert error.value.reason == message
    assert str(error.value) == message


def test_sales_receipt_of_exactly_seven_hundred_can_go_without_document() -> None:
    customer = validate_customer(
        InvoiceKind.SALES_RECEIPT, Customer(DocumentType.NONE, "999", "  Ana  "), ANONYMOUS_LIMIT
    )

    # Sin documento no se guarda un número suelto, pero sí el nombre que dieron.
    assert customer == Customer(DocumentType.NONE, "", "Ana", "")


def test_identified_sales_receipt_accepts_dni_ce_and_ruc() -> None:
    dni = validate_customer(
        InvoiceKind.SALES_RECEIPT, Customer(DocumentType.DNI, "12345678", "Ana"), D("900")
    )
    ce = validate_customer(
        InvoiceKind.SALES_RECEIPT, Customer(DocumentType.CE, " ab12345678 ", "Li"), D("1")
    )
    ruc = validate_customer(
        InvoiceKind.SALES_RECEIPT, Customer(DocumentType.RUC, "10123456789", "Luis"), D("1")
    )

    assert (dni.document_number, ce.document_number, ruc.document_number) == (
        "12345678",
        "AB12345678",
        "10123456789",
    )


def test_customer_name_and_address_are_trimmed() -> None:
    customer = validate_customer(
        InvoiceKind.SALES_RECEIPT, Customer(DocumentType.NONE, "", "N" * 120, "A" * 250), D("1")
    )

    assert (len(customer.name), len(customer.address)) == (100, 200)


# -- Comprobante ----------------------------------------------------------------------------


def test_supplier_result_is_saved_trimmed() -> None:
    invoice = _tax_invoice()

    invoice.record_result(
        InvoiceStatus.REJECTED, "m" * 400, "https://pdf.test/" + "u" * 600, {"errors": "x"}
    )

    assert invoice.status is InvoiceStatus.REJECTED
    assert (len(invoice.provider_message), len(invoice.pdf_url)) == (300, 500)
    assert invoice.pdf_url.startswith("https://pdf.test/uuu")
    assert invoice.provider_response == {"errors": "x"}


@pytest.mark.parametrize(
    "link",
    [
        "http://nubefact.test/f001-1.pdf",
        "javascript:alert(1)",
        "data:text/html,hola",
        "//nubefact.test/f001-1.pdf",
        "https:///f001-1.pdf",
        "https://nubefact.test/f001 1.pdf",
        "https://[nubefact.test/f001-1.pdf",
    ],
)
def test_pdf_link_that_is_not_https_is_not_saved(link: str) -> None:
    """La pantalla abre el enlace tal cual: el proveedor no elige a dónde lleva."""
    invoice = _tax_invoice()

    invoice.record_result(InvoiceStatus.ACCEPTED, "Aceptado", link, {})

    assert invoice.pdf_url == ""


def test_https_pdf_link_is_saved_as_is() -> None:
    invoice = _tax_invoice()

    invoice.record_result(InvoiceStatus.ACCEPTED, "Aceptado", " HTTPS://nubefact.test/a.pdf ", {})

    assert invoice.pdf_url == "HTTPS://nubefact.test/a.pdf"


@pytest.mark.parametrize(
    "status", [InvoiceStatus.PENDING, InvoiceStatus.REJECTED, InvoiceStatus.SIMULATED]
)
def test_only_the_accepted_is_not_resent(status: InvoiceStatus) -> None:
    _tax_invoice(status=status).ensure_resendable()

    with pytest.raises(InvalidInvoiceError) as error:
        _tax_invoice(status=InvoiceStatus.ACCEPTED).ensure_resendable()
    assert str(error.value) == "El comprobante F001-15 ya fue aceptado por SUNAT."


def test_lines_leave_out_complimentary_items_and_round() -> None:
    lines = build_lines(
        [
            ("Lomo saltado", 2, D("28.5"), False),
            ("Chicha", 1, D("5.00"), True),
            ("P" * 300, 3, D("1.335"), False),
        ]
    )

    assert [(ln.description[:12], ln.quantity, ln.unit_price, ln.total) for ln in lines] == [
        ("Lomo saltado", 2, D("28.50"), D("57.00")),
        ("PPPPPPPPPPPP", 3, D("1.34"), D("4.00")),
    ]
    assert len(lines[1].description) == 250


def test_default_issue_date_is_now_in_utc() -> None:
    before = SystemClock().now()

    assert _tax_invoice().issued_at >= before


# -- URL del proveedor ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://api.nubefact.com/a b", "La URL del proveedor no puede llevar espacios."),
        ("https://api.nubefact.com/\x00", "La URL del proveedor no puede llevar espacios."),
        ("https://api.nubefact.com:99999/x", "La URL del proveedor no es válida."),
        ("http://api.nubefact.com/x", "La URL del proveedor tiene que empezar con https://."),
        (
            "https://u:p@api.nubefact.com/x",
            "La URL del proveedor no puede llevar usuario ni contraseña; el token va aparte.",
        ),
        (
            "https://u@api.nubefact.com/x",
            "La URL del proveedor no puede llevar usuario ni contraseña; el token va aparte.",
        ),
        ("https:///x", "La URL del proveedor no tiene servidor."),
        ("https://./x", "La URL del proveedor no tiene servidor."),
        ("https://localhost/x", "La URL del proveedor no puede apuntar a una dirección interna."),
        (
            "https://api.localhost./x",
            "La URL del proveedor no puede apuntar a una dirección interna.",
        ),
        ("https://db.internal/x", "La URL del proveedor no puede apuntar a una dirección interna."),
        ("https://nas.local/x", "La URL del proveedor no puede apuntar a una dirección interna."),
        (
            "https://192.168.1.10/x",
            "La URL del proveedor no puede apuntar a una dirección interna.",
        ),
        ("https://[fd00::1]/x", "La URL del proveedor no puede apuntar a una dirección interna."),
    ],
)
def test_each_rejected_url_has_its_reason(url: str, reason: str) -> None:
    assert provider_url_problem(url) == reason
    with pytest.raises(InvalidBillingSettingsError) as error:
        validate_provider_url(url)
    assert error.value.reason == reason


def test_valid_url_is_returned_without_surrounding_spaces() -> None:
    assert validate_provider_url("  https://api.nubefact.com/x  ") == "https://api.nubefact.com/x"
    assert validate_provider_url("   ") == ""
    assert provider_url_problem("HTTPS://API.NUBEFACT.COM/x") is None
    assert provider_url_problem("https://1.1.1.1/x") is None
    assert provider_url_problem("https://api.nubefact.com./x") is None


# -- Errores -------------------------------------------------------------------------------


def test_invoice_errors_explain_what_happened() -> None:
    not_found = InvoiceNotFoundError(4)
    not_paid = PaidOrderNotFoundError(7)
    already_invoiced = OrderAlreadyInvoicedError("B001-3")

    assert (str(not_found), not_found.invoice_id) == ("No existe el comprobante 4.", 4)
    assert (str(not_paid), not_paid.order_id) == ("El pedido 7 no existe o no está pagado.", 7)
    assert (str(already_invoiced), already_invoiced.invoice_code) == (
        "El pedido ya tiene el comprobante B001-3.",
        "B001-3",
    )
    assert str(InvoiceNumberTakenError()) == (
        "Otro comprobante tomó el mismo número a la vez. Vuelve a intentarlo."
    )
