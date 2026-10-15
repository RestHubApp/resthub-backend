"""Límites y mensajes del dominio de comprobantes: datos fiscales, credenciales, IGV y cliente.

Complementa `test_billing.py`: acá se fija cada mensaje de SUNAT que ve el
encargado, el redondeo del IGV al medio céntimo, las credenciales SOL del local
y los documentos que guarda un comprobante.
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
    split_igv,
    validate_customer,
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
        sol_user=" moddatos ",
        sol_password="clave",
        certificate_pem="-----BEGIN PRIVATE KEY-----",
    )

    assert settings.ruc == "20123456789"
    assert settings.legal_name == "Doña Rosa SAC"
    assert settings.address == "Jr. Pizarro 450"
    assert str(settings.igv_rate) == "10.50"
    assert (settings.sales_receipt_series, settings.tax_invoice_series) == ("B002", "F00A")
    # El usuario SOL secundario va en mayúsculas, como lo da SUNAT.
    assert settings.sol_user == "MODDATOS"
    assert (settings.has_sol_credentials, settings.has_certificate) == (True, True)
    assert settings.can_register
    assert settings.is_ready


_READY_DATA = {
    "ruc": "20123456789",
    "legal_name": "Rosa SAC",
    "sol_user": "MODDATOS",
    "sol_password": "moddatos",
    "certificate_pem": "-----BEGIN PRIVATE KEY-----",
}


@pytest.mark.parametrize("missing", list(_READY_DATA))
def test_missing_any_field_means_not_really_ready_to_issue(missing: str) -> None:
    data = {**_READY_DATA, missing: ""}

    assert not BillingSettings(restaurant_id=1, **data).is_ready  # type: ignore[arg-type]


def test_registering_does_not_need_the_legal_name_but_issuing_does() -> None:
    """Lycet firma con RUC, usuario SOL y certificado; la razón social va en el comprobante."""
    settings = BillingSettings(restaurant_id=1, **{**_READY_DATA, "legal_name": ""})  # type: ignore[arg-type]

    assert settings.can_register
    assert not settings.is_ready


@pytest.mark.parametrize("missing", ["sol_user", "sol_password"])
def test_half_of_the_sol_credentials_are_not_credentials(missing: str) -> None:
    settings = BillingSettings(restaurant_id=1, **{**_READY_DATA, missing: ""})  # type: ignore[arg-type]

    assert not settings.has_sol_credentials
    assert not settings.can_register


@pytest.mark.parametrize("sol_user", ["AB", "MOD-DATOS", "MOD DATOS", "Ñ" * 5, "A" * 21])
def test_invalid_sol_user_explains_the_reason(sol_user: str) -> None:
    with pytest.raises(InvalidBillingSettingsError) as error:
        BillingSettings(restaurant_id=1, sol_user=sol_user)

    assert error.value.reason == (
        "El usuario SOL son de 3 a 20 letras o números, sin el RUC delante."
    )


def test_too_long_sol_password_is_rejected() -> None:
    with pytest.raises(InvalidBillingSettingsError) as error:
        BillingSettings(restaurant_id=1, sol_password="x" * 101)

    assert error.value.reason == "La clave SOL es demasiado larga."
    assert BillingSettings(restaurant_id=1, sol_password="x" * 100).sol_password == "x" * 100


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


def test_provider_result_keeps_the_documents_and_trims_the_message() -> None:
    invoice = _tax_invoice()

    invoice.record_result(
        InvoiceStatus.ACCEPTED,
        "m" * 400,
        {"sunatResponse": {"success": True}},
        signed_xml="<Invoice/>",
        cdr_zip="UEsDBA==",
        document_payload={"serie": "F001"},
    )

    assert invoice.status is InvoiceStatus.ACCEPTED
    assert len(invoice.provider_message) == 300
    assert invoice.provider_response == {"sunatResponse": {"success": True}}
    assert (invoice.signed_xml, invoice.cdr_zip) == ("<Invoice/>", "UEsDBA==")
    assert invoice.document_payload == {"serie": "F001"}
    assert (invoice.has_xml, invoice.has_cdr, invoice.has_pdf) == (True, True, True)
    assert invoice.has_documents_to_archive


def test_a_new_result_replaces_the_documents_of_the_previous_one() -> None:
    invoice = _tax_invoice()
    invoice.record_result(
        InvoiceStatus.ACCEPTED, "Aceptado", {}, signed_xml="<a/>", cdr_zip="UEsDBA=="
    )

    invoice.record_result(InvoiceStatus.PENDING, "Sin conexión", {})

    assert (invoice.signed_xml, invoice.cdr_zip, invoice.document_payload) == ("", "", {})
    assert (invoice.has_xml, invoice.has_cdr, invoice.has_pdf) == (False, False, False)


@pytest.mark.parametrize(
    ("status", "signed_xml", "expected"),
    [
        (InvoiceStatus.ACCEPTED, "<Invoice/>", True),
        (InvoiceStatus.ACCEPTED, "", False),
        (InvoiceStatus.REJECTED, "<Invoice/>", False),
        (InvoiceStatus.PENDING, "<Invoice/>", False),
    ],
)
def test_only_an_accepted_invoice_with_xml_is_archived(
    status: InvoiceStatus, signed_xml: str, expected: bool
) -> None:
    invoice = _tax_invoice()
    invoice.record_result(status, "", {}, signed_xml=signed_xml)

    assert invoice.has_documents_to_archive is expected


@pytest.mark.parametrize(
    "status", [InvoiceStatus.PENDING, InvoiceStatus.REJECTED, InvoiceStatus.SIMULATED]
)
def test_only_the_accepted_is_not_resent(status: InvoiceStatus) -> None:
    _tax_invoice(status=status).ensure_resendable()

    with pytest.raises(InvalidInvoiceError) as error:
        _tax_invoice(status=InvoiceStatus.ACCEPTED).ensure_resendable()
    assert str(error.value) == "El comprobante F001-15 ya fue aceptado por SUNAT."


def test_lines_keep_courtesies_as_free_and_round() -> None:
    """Una cortesía va como transferencia gratuita: total cero y su precio de referencia."""
    lines = build_lines(
        [
            ("Lomo saltado", 2, D("28.5"), False),
            ("Chicha", 1, D("5.00"), True),
            ("P" * 300, 3, D("1.335"), False),
        ]
    )

    assert [
        (ln.description[:12], ln.quantity, ln.unit_price, ln.total, ln.is_courtesy) for ln in lines
    ] == [
        ("Lomo saltado", 2, D("28.50"), D("57.00"), False),
        ("Chicha", 1, D("5.00"), D("0.00"), True),
        ("PPPPPPPPPPPP", 3, D("1.34"), D("4.00"), False),
    ]
    assert len(lines[2].description) == 250


def test_default_issue_date_is_now_in_utc() -> None:
    before = SystemClock().now()

    assert _tax_invoice().issued_at >= before


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
