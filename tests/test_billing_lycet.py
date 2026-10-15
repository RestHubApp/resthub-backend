"""El adaptador de Lycet sin la aplicación, el importe en letras y el certificado digital.

Se prueban el JSON de Greenter, la lectura de la respuesta y las llamadas HTTP.

Las respuestas son las grabadas de un Lycet local contra SUNAT beta (`tests/fixtures/lycet/`).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from resthub.modules.billing.adapters.sunat.certificates import CryptographyCertificateReader
from resthub.modules.billing.adapters.sunat.lycet import (
    API_PATH,
    LycetGateway,
    build_payload,
    interpret,
)
from resthub.modules.billing.domain.amount_words import amount_in_words, integer_in_words
from resthub.modules.billing.domain.exceptions import (
    BillingProviderError,
    InvalidCertificateError,
)
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    build_lines,
)
from tests.fakes import FIXED_NOW
from tests.fakes_lycet import (
    LYCET_TOKEN,
    LYCET_URL,
    PDF,
    PFX_PASSWORD,
    FakeLycet,
    lycet_reply,
    make_certificate,
)

D = Decimal

SETTINGS = BillingSettings(
    restaurant_id=1,
    ruc="20123456789",
    legal_name="Cevichería Doña Rosa S.A.C.",
    address="Av. Larco 123, Trujillo",
    sol_user="MODDATOS",
    sol_password="moddatos",
    certificate_pem="pem-de-prueba\n",
)


def _invoice(
    *,
    kind: InvoiceKind = InvoiceKind.SALES_RECEIPT,
    customer: Customer | None = None,
    items: list[tuple[str, int, Decimal, bool]] | None = None,
    total: str = "56.00",
    discount: str = "0.00",
) -> Invoice:
    return Invoice(
        restaurant_id=1,
        order_id=7,
        kind=kind,
        series="B001" if kind is InvoiceKind.SALES_RECEIPT else "F001",
        number=15,
        customer=customer or Customer(),
        lines=build_lines(items or [("Lomo saltado", 2, D("28.00"), False)]),
        total=D(total),
        discount=D(discount),
        igv_rate=D("18.00"),
        issued_by=1,
        issued_at=FIXED_NOW,
    )


# -- El JSON de Greenter ----------------------------------------------------------------


def test_sales_receipt_payload_has_the_document_and_totals() -> None:
    payload = build_payload(SETTINGS, _invoice())

    assert (payload["tipoDoc"], payload["serie"], payload["correlativo"]) == ("03", "B001", "15")
    assert (payload["ublVersion"], payload["tipoOperacion"], payload["tipoMoneda"]) == (
        "2.1",
        "0101",
        "PEN",
    )
    assert payload["company"]["ruc"] == "20123456789"
    assert payload["company"]["razonSocial"] == "Cevichería Doña Rosa S.A.C."
    assert payload["company"]["address"]["direccion"] == "Av. Larco 123, Trujillo"
    assert payload["company"]["address"]["codLocal"] == "0000"
    # 56.00 con IGV: 47.46 de base y 8.54 de IGV.
    assert (payload["mtoOperGravadas"], payload["mtoIGV"], payload["totalImpuestos"]) == (
        47.46,
        8.54,
        8.54,
    )
    assert (payload["valorVenta"], payload["subTotal"], payload["mtoImpVenta"]) == (
        47.46,
        56.0,
        56.0,
    )
    assert payload["legends"] == [
        {"code": "1000", "value": "SON CINCUENTA Y SEIS CON 00/100 SOLES"}
    ]
    assert "descuentos" not in payload
    assert "mtoOperGratuitas" not in payload
    assert payload["observacion"] == "Pedido 7"
    # Viaja como JSON: nada de `Decimal`.
    json.dumps(payload)


def test_tax_invoice_payload_has_type_01() -> None:
    customer = Customer(DocumentType.RUC, "20987654321", "Empresa SAC", "Av. Grau 1")

    payload = build_payload(SETTINGS, _invoice(kind=InvoiceKind.TAX_INVOICE, customer=customer))

    assert (payload["tipoDoc"], payload["serie"]) == ("01", "F001")
    assert payload["formaPago"] == {"moneda": "PEN", "tipo": "Contado"}


@pytest.mark.parametrize(
    ("customer", "expected"),
    [
        (Customer(), {"tipoDoc": "-", "numDoc": "-", "rznSocial": "Clientes varios"}),
        (
            Customer(DocumentType.NONE, "", "Ana", "Jr. Uno 1"),
            {"tipoDoc": "-", "numDoc": "-", "rznSocial": "Ana"},
        ),
        (
            Customer(DocumentType.DNI, "45678912", "Ana Torres"),
            {"tipoDoc": "1", "numDoc": "45678912", "rznSocial": "Ana Torres"},
        ),
        (
            Customer(DocumentType.RUC, "20987654321", "Empresa SAC", "Av. Grau 1"),
            {
                "tipoDoc": "6",
                "numDoc": "20987654321",
                "rznSocial": "Empresa SAC",
                "address": {"direccion": "Av. Grau 1", "codigoPais": "PE"},
            },
        ),
    ],
    ids=["generic", "generic-with-address", "dni", "ruc-with-address"],
)
def test_customer_party(customer: Customer, expected: dict[str, Any]) -> None:
    assert build_payload(SETTINGS, _invoice(customer=customer))["client"] == expected


def test_each_line_separates_its_igv() -> None:
    (detail,) = build_payload(SETTINGS, _invoice())["details"]

    assert detail == {
        "unidad": "NIU",
        "cantidad": 2,
        "codProducto": "P001",
        "descripcion": "Lomo saltado",
        "mtoValorVenta": 47.46,
        "mtoBaseIgv": 47.46,
        "porcentajeIgv": 18.0,
        "igv": 8.54,
        "totalImpuestos": 8.54,
        "tipAfeIgv": "10",
        # 28.00 / 1.18 con seis decimales.
        "mtoValorUnitario": 23.728814,
        "mtoPrecioUnitario": 28.0,
    }


def test_order_discount_goes_as_a_global_discount_that_lowers_the_base() -> None:
    # 56.00 - 10 % = 50.40 cobrado: base 42.71.
    payload = build_payload(SETTINGS, _invoice(total="50.40", discount="5.60"))

    (discount,) = payload["descuentos"]
    assert discount["codTipo"] == "02"
    assert discount["montoBase"] == 47.46
    # La base de las líneas menos el descuento es la base imponible.
    assert D(str(discount["montoBase"])) - D(str(discount["monto"])) == D("42.71")
    assert discount["factor"] == float((D("4.75") / D("47.46")).quantize(D("0.00001")))
    assert (payload["mtoOperGravadas"], payload["mtoIGV"], payload["mtoImpVenta"]) == (
        42.71,
        7.69,
        50.4,
    )
    assert payload["legends"][0]["value"] == "SON CINCUENTA CON 40/100 SOLES"


def test_courtesy_goes_as_a_free_transfer() -> None:
    payload = build_payload(
        SETTINGS,
        _invoice(
            items=[
                ("Lomo saltado", 2, D("28.00"), False),
                ("Postre de la casa", 1, D("11.80"), True),
            ]
        ),
    )

    paid, free = payload["details"]
    assert paid["tipAfeIgv"] == "10"
    assert free["tipAfeIgv"] == "13"
    assert (free["mtoValorUnitario"], free["mtoPrecioUnitario"]) == (0, 0)
    assert free["mtoValorGratuito"] == 10.0
    assert (free["mtoValorVenta"], free["igv"]) == (10.0, 1.8)
    assert (payload["mtoOperGratuitas"], payload["mtoIGVGratuitas"]) == (10.0, 1.8)
    # Lo gratuito no suma a lo cobrado ni a la base.
    assert (payload["mtoOperGravadas"], payload["mtoImpVenta"]) == (47.46, 56.0)
    assert {
        "code": "1002",
        "value": ("TRANSFERENCIA GRATUITA DE UN BIEN Y/O SERVICIO PRESTADO GRATUITAMENTE"),
    } in payload["legends"]
    assert "descuentos" not in payload


def test_issue_date_carries_the_local_offset() -> None:
    lima = build_payload(SETTINGS, _invoice())
    bogota = build_payload(
        BillingSettings(restaurant_id=1, ruc="20123456789", timezone="America/Bogota"), _invoice()
    )

    # 12:00 UTC del 15 de junio de 2026.
    assert lima["fechaEmision"] == "2026-06-15T07:00:00-05:00"
    assert bogota["fechaEmision"] == "2026-06-15T07:00:00-05:00"
    madrid = build_payload(BillingSettings(restaurant_id=1, timezone="Europe/Madrid"), _invoice())
    assert madrid["fechaEmision"] == "2026-06-15T14:00:00+02:00"


# -- La respuesta de Lycet ---------------------------------------------------------------


def test_accepted_reply_keeps_the_documents_apart_from_the_raw_answer() -> None:
    reply = lycet_reply("accepted")

    result = interpret(200, reply)

    assert result.status is InvoiceStatus.ACCEPTED
    assert result.message == "Aceptado por SUNAT: La Boleta numero B001-901, ha sido aceptada"
    assert result.signed_xml == reply["xml"]
    assert result.cdr_zip == reply["sunatResponse"]["cdrZip"]
    assert result.raw is not None
    assert "xml" not in result.raw
    assert "cdrZip" not in result.raw["sunatResponse"]
    assert result.raw["hash"] == reply["hash"]
    assert result.raw["sunatResponse"]["cdrResponse"]["code"] == "0"


def test_observed_reply_is_accepted_with_short_observations() -> None:
    result = interpret(200, lycet_reply("observed"))

    assert result.status is InvoiceStatus.ACCEPTED
    assert result.message == (
        "Aceptado con observaciones: 4312 - El importe total del comprobante no coincide "
        "con el valor calculado"
    )
    assert result.cdr_zip


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (
            "rejected_2800",
            (
                "Rechazado por SUNAT (2800): El dato ingresado en el tipo de documento de "
                "identidad del receptor no esta permitido."
            ),
        ),
        ("rejected_0151", "Rechazado por SUNAT (0151): El nombre del archivo ZIP es incorrecto"),
    ],
)
def test_rejected_reply_says_why_without_the_technical_detail(name: str, message: str) -> None:
    result = interpret(200, lycet_reply(name))

    assert result.status is InvoiceStatus.REJECTED
    assert result.message == message
    assert "Detalle" not in result.message
    assert result.cdr_zip == ""
    assert result.signed_xml


def test_rejected_cdr_with_code_between_2000_and_3999() -> None:
    reply = {
        "xml": "<Invoice/>",
        "sunatResponse": {
            "success": True,
            "cdrZip": "UEsDBA==",
            "cdrResponse": {"code": "2017", "description": "El numero de RUC no existe"},
        },
    }

    result = interpret(200, reply)

    assert result.status is InvoiceStatus.REJECTED
    assert result.message == "Rechazado por SUNAT (2017): El numero de RUC no existe"
    assert result.cdr_zip == "UEsDBA=="


@pytest.mark.parametrize("code", ["0109", "0130", "HTTP", ""])
def test_sunat_not_answering_leaves_it_pending(code: str) -> None:
    reply = {
        "xml": "<Invoice/>",
        "sunatResponse": {"success": False, "error": {"code": code, "message": "Servicio caído"}},
    }

    result = interpret(200, reply)

    assert result.status is InvoiceStatus.PENDING
    assert "Reenvíalo" in result.message
    assert result.signed_xml == "<Invoice/>"


def test_lycet_failing_leaves_it_pending() -> None:
    failed = interpret(502, {})
    empty = interpret(200, {"xml": "<Invoice/>"})

    assert failed.status is InvoiceStatus.PENDING
    assert failed.message == (
        "El servicio de facturación no pudo firmar ni enviar el comprobante (HTTP 502): "
        "revisa el usuario SOL y el certificado del local y reenvíalo."
    )
    assert empty.status is InvoiceStatus.PENDING
    assert empty.message == "No se pudo conectar con el servicio de facturación; se puede reenviar."


# -- Las llamadas a Lycet ------------------------------------------------------------------


async def test_unconfigured_lycet_simulates_without_calling() -> None:
    for gateway in (LycetGateway("", "token"), LycetGateway(LYCET_URL, "  ")):
        result = await gateway.send(SETTINGS, _invoice(), InvoiceKind.SALES_RECEIPT)

        assert result.status is InvoiceStatus.SIMULATED
        assert result.message == (
            "El servidor no tiene configurado el servicio de facturación: no se envió."
        )
        assert result.document_payload == {}
        # Registrar u olvidar sin servicio no hace nada.
        await gateway.register(SETTINGS)
        await gateway.unregister("20123456789")


async def test_send_posts_the_payload_with_the_token_in_the_query() -> None:
    fake = FakeLycet()

    result = await fake.gateway().send(SETTINGS, _invoice(), InvoiceKind.SALES_RECEIPT)

    assert result.status is InvoiceStatus.ACCEPTED
    ((method, path, body),) = fake.calls
    assert (method, path) == ("POST", "/invoice/send")
    assert body == build_payload(SETTINGS, _invoice())
    assert result.document_payload == body
    assert fake.tokens == [LYCET_TOKEN]


async def test_trailing_slash_in_the_url_is_ignored() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=lycet_reply("accepted"))

    gateway = LycetGateway(f"{LYCET_URL}/", LYCET_TOKEN, httpx.MockTransport(handler))
    await gateway.send(SETTINGS, _invoice(), InvoiceKind.SALES_RECEIPT)

    assert paths == [f"{API_PATH}/invoice/send"]


def _broken(request: httpx.Request) -> httpx.Response:
    message = "sin red"
    raise httpx.ConnectError(message, request=request)


async def test_network_error_leaves_it_pending_and_keeps_the_document() -> None:
    gateway = LycetGateway(LYCET_URL, LYCET_TOKEN, httpx.MockTransport(_broken))

    result = await gateway.send(SETTINGS, _invoice(), InvoiceKind.SALES_RECEIPT)

    assert result.status is InvoiceStatus.PENDING
    assert (
        result.message == "No se pudo conectar con el servicio de facturación; se puede reenviar."
    )
    assert result.document_payload["serie"] == "B001"


async def test_non_json_reply_leaves_it_pending() -> None:
    gateway = LycetGateway(
        LYCET_URL,
        LYCET_TOKEN,
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"<html>")),
    )

    result = await gateway.send(SETTINGS, _invoice(), InvoiceKind.SALES_RECEIPT)

    assert result.status is InvoiceStatus.PENDING


async def test_render_pdf_posts_the_document_that_was_sent() -> None:
    fake = FakeLycet()
    invoice = _invoice()
    invoice.document_payload = {"serie": "B001", "correlativo": "15"}

    pdf = await fake.gateway().render_pdf(invoice)

    assert pdf == PDF
    assert fake.calls == [("POST", "/invoice/pdf", {"serie": "B001", "correlativo": "15"})]


async def test_render_pdf_without_document_or_service_does_not_call() -> None:
    fake = FakeLycet()
    sent = _invoice()
    sent.document_payload = {"serie": "B001"}

    with pytest.raises(BillingProviderError):
        await fake.gateway().render_pdf(_invoice())
    with pytest.raises(BillingProviderError):
        await LycetGateway("", "").render_pdf(sent)
    assert fake.calls == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, content=b"error"),
        httpx.Response(200, json={"no": "es un pdf"}),
        httpx.Response(
            200, content=b"0" * (5 * 1024 * 1024 + 1), headers={"content-type": "application/pdf"}
        ),
    ],
    ids=["http-error", "not-a-pdf", "too-large"],
)
async def test_render_pdf_failures_are_provider_errors(response: httpx.Response) -> None:
    gateway = LycetGateway(LYCET_URL, LYCET_TOKEN, httpx.MockTransport(lambda _: response))
    invoice = _invoice()
    invoice.document_payload = {"serie": "B001"}

    with pytest.raises(BillingProviderError):
        await gateway.render_pdf(invoice)


async def test_render_pdf_unreachable_is_a_provider_error() -> None:
    gateway = LycetGateway(LYCET_URL, LYCET_TOKEN, httpx.MockTransport(_broken))
    invoice = _invoice()
    invoice.document_payload = {"serie": "B001"}

    with pytest.raises(BillingProviderError) as error:
        await gateway.render_pdf(invoice)
    assert "conectar" in error.value.reason


async def test_register_sends_ruc_user_password_and_certificate() -> None:
    fake = FakeLycet()

    await fake.gateway().register(SETTINGS)

    ((method, path, body),) = fake.calls
    assert (method, path) == ("PUT", "/configuration/company/20123456789")
    assert body == {
        "SOL_USER": "20123456789MODDATOS",
        "SOL_PASS": "moddatos",
        "certificate": base64.b64encode(b"pem-de-prueba\n").decode(),
    }


@pytest.mark.parametrize("transport", ["refused", "unreachable"])
async def test_register_failure_is_a_provider_error(transport: str) -> None:
    gateway = (
        FakeLycet(register_status=502).gateway()
        if transport == "refused"
        else LycetGateway(LYCET_URL, LYCET_TOKEN, httpx.MockTransport(_broken))
    )

    with pytest.raises(BillingProviderError):
        await gateway.register(SETTINGS)


async def test_unregister_never_raises() -> None:
    fake = FakeLycet(register_status=500)

    await fake.gateway().unregister("20123456789")
    await FakeLycet(register_status=404).gateway().unregister("20123456789")
    await LycetGateway(LYCET_URL, LYCET_TOKEN, httpx.MockTransport(_broken)).unregister("2")

    assert fake.calls == [("DELETE", "/configuration/company/20123456789", None)]


# -- Importe en letras ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "words"),
    [
        (0, "CERO"),
        (1, "UNO"),
        (15, "QUINCE"),
        (21, "VEINTIUNO"),
        (30, "TREINTA"),
        (45, "CUARENTA Y CINCO"),
        (100, "CIEN"),
        (101, "CIENTO UNO"),
        (500, "QUINIENTOS"),
        (999, "NOVECIENTOS NOVENTA Y NUEVE"),
        (1000, "MIL"),
        (1001, "MIL UNO"),
        (21000, "VEINTIUN MIL"),
        (31000, "TREINTA Y UN MIL"),
        (100_000, "CIEN MIL"),
        (1_000_000, "UN MILLON"),
        (2_500_000, "DOS MILLONES QUINIENTOS MIL"),
        (21_000_000, "VEINTIUN MILLONES"),
    ],
)
def test_integer_in_words(number: int, words: str) -> None:
    assert integer_in_words(number) == words


@pytest.mark.parametrize(
    ("amount", "words"),
    [
        ("0", "SON CERO CON 00/100 SOLES"),
        ("118", "SON CIENTO DIECIOCHO CON 00/100 SOLES"),
        ("50.4", "SON CINCUENTA CON 40/100 SOLES"),
        ("0.995", "SON UNO CON 00/100 SOLES"),
        ("2500000.5", "SON DOS MILLONES QUINIENTOS MIL CON 50/100 SOLES"),
    ],
)
def test_amount_in_words(amount: str, words: str) -> None:
    assert amount_in_words(D(amount)) == words


# -- Certificado digital -------------------------------------------------------------------

READER = CryptographyCertificateReader()


def test_pfx_opens_with_its_password() -> None:
    pfx, pem = make_certificate(expires_at=datetime(2028, 3, 1, tzinfo=UTC))

    parsed = READER.read(pfx, PFX_PASSWORD)

    assert parsed.expires_at == datetime(2028, 3, 1, tzinfo=UTC)
    assert parsed.subject == "CEVICHERIA DONA ROSA SAC"
    assert "BEGIN PRIVATE KEY" in parsed.pem
    assert "ENCRYPTED" not in parsed.pem
    assert pem.split(b"-----BEGIN CERTIFICATE-----")[1].decode() in parsed.pem


def test_pem_opens_without_password() -> None:
    _, pem = make_certificate()

    parsed = READER.read(b"\n" + pem, "")

    assert parsed.expires_at == datetime(2030, 1, 1, tzinfo=UTC)
    assert "BEGIN CERTIFICATE" in parsed.pem


def _expect(content: bytes, password: str, message: str) -> None:
    with pytest.raises(InvalidCertificateError) as error:
        READER.read(content, password)
    assert error.value.reason == message


def test_wrong_password_or_damaged_file() -> None:
    pfx, _ = make_certificate()
    message = "No se pudo abrir el certificado: revisa el archivo y su contraseña."

    _expect(pfx, "otra", message)
    _expect(b"no es un certificado", "", message)


def test_empty_file() -> None:
    _expect(b"", "", "El archivo del certificado está vacío.")


def test_pem_without_its_private_key() -> None:
    _, pem = make_certificate()
    only_certificate = b"-----BEGIN CERTIFICATE-----" + pem.split(b"-----BEGIN CERTIFICATE-----")[1]

    _expect(
        only_certificate,
        "",
        "El certificado no trae su llave privada: sube el .pfx o .p12 completo.",
    )


def test_pem_without_certificate() -> None:
    _, pem = make_certificate()
    only_key = pem.split(b"-----BEGIN CERTIFICATE-----")[0]

    _expect(only_key, "", "El archivo .pem no trae un certificado válido.")


def test_key_that_does_not_match_the_certificate() -> None:
    _, first = make_certificate()
    _, second = make_certificate()
    marker = b"-----BEGIN CERTIFICATE-----"
    mixed = first.split(marker)[0] + marker + second.split(marker)[1]

    _expect(mixed, "", "La llave privada no corresponde al certificado.")


def test_non_rsa_certificate_is_refused() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "EC")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(FIXED_NOW - timedelta(days=1))
        .not_valid_after(FIXED_NOW + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ) + certificate.public_bytes(serialization.Encoding.PEM)

    _expect(pem, "", "El certificado no es RSA: SUNAT no lo acepta para firmar.")
