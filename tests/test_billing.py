"""Comprobantes electrónicos: IGV, reglas de SUNAT, credenciales del local y el envío por Lycet."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import Permission
from resthub.modules.billing.adapters.sunat.lycet import build_payload
from resthub.modules.billing.domain.exceptions import InvalidInvoiceError
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    build_lines,
    split_igv,
    validate_customer,
)
from tests.builders import SeededMenu, open_cash_session, seed_menu
from tests.conftest import (
    StaffedRestaurant,
    authorization_for,
    staff_with_role,
    staffed_restaurant,
    url,
)
from tests.fakes_lycet import (
    LYCET_TOKEN,
    PDF,
    FakeLycet,
    certificate_upload,
    configure_billing,
    lycet_reply,
    make_certificate,
    paid_order,
    use_fake_lycet,
)

BILLING_URL = url("billing")
ORDERS_URL = url("orders")
SETTINGS_URL = f"{BILLING_URL}/settings"
CERTIFICATE_URL = f"{BILLING_URL}/settings/certificate"


# -- Dominio ------------------------------------------------------------------


def test_igv_is_split_from_a_price_that_already_includes_it() -> None:
    assert split_igv(Decimal("118.00"), Decimal(18)) == (Decimal("100.00"), Decimal("18.00"))
    assert split_igv(Decimal("61.50"), Decimal("10.5")) == (Decimal("55.66"), Decimal("5.84"))


@pytest.mark.parametrize(
    ("kind", "customer", "total"),
    [
        (InvoiceKind.TAX_INVOICE, Customer(DocumentType.DNI, "12345678", "Ana"), "50"),
        (InvoiceKind.TAX_INVOICE, Customer(DocumentType.RUC, "20123456789", ""), "50"),
        (InvoiceKind.SALES_RECEIPT, Customer(), "700.01"),
        (InvoiceKind.SALES_RECEIPT, Customer(DocumentType.DNI, "1234", "Ana"), "50"),
    ],
    ids=[
        "tax-invoice-without-ruc",
        "tax-invoice-without-legal-name",
        "large-anonymous-receipt",
        "short-dni",
    ],
)
def test_sunat_rules_on_the_customer(kind: InvoiceKind, customer: Customer, total: str) -> None:
    with pytest.raises(InvalidInvoiceError):
        validate_customer(kind, customer, Decimal(total))


def test_small_sales_receipt_can_go_to_generic_customers() -> None:
    customer = validate_customer(InvoiceKind.SALES_RECEIPT, Customer(), Decimal("699.99"))

    assert customer.name == "Clientes varios"


def test_issue_date_is_the_restaurants_local_day() -> None:
    # 21:30 del 1 de octubre en Lima ya son las 02:30 del 2 en UTC.
    sales_receipt = Invoice(
        restaurant_id=1,
        order_id=1,
        kind=InvoiceKind.SALES_RECEIPT,
        series="B001",
        number=1,
        customer=Customer(),
        lines=build_lines([("Lomo saltado", 1, Decimal("28.00"), False)]),
        total=Decimal("28.00"),
        discount=Decimal("0.00"),
        igv_rate=Decimal("18.00"),
        issued_by=1,
        issued_at=datetime(2026, 10, 2, 2, 30, tzinfo=UTC),
    )
    billing_settings = BillingSettings(restaurant_id=1, timezone="America/Lima")

    payload = build_payload(billing_settings, sales_receipt)

    assert payload["fechaEmision"] == "2026-10-01T21:30:00-05:00"


# -- HTTP: datos fiscales y credenciales -----------------------------------------


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    menu = await seed_menu(session, restaurant_a.id)
    await open_cash_session(session, restaurant_a.id, restaurant_a.admin.id or 0)
    return menu


@pytest.fixture
def lycet(app: FastAPI) -> FakeLycet:
    fake = FakeLycet()
    use_fake_lycet(app, fake)
    return fake


def _settings_body(**extra: Any) -> dict[str, Any]:
    return {
        "ruc": "20123456789",
        "legal_name": "Cevichería Doña Rosa S.A.C.",
        "address": "Av. Larco 123, Trujillo",
        "igv_rate": "18.00",
        "boleta_series": "B001",
        "factura_series": "F001",
        **extra,
    }


async def _issue(
    client: AsyncClient, restaurant: StaffedRestaurant, order_id: int, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": order_id, "kind": "boleta", **extra},
        headers=authorization_for(restaurant.waiter),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_settings_never_return_the_credentials(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    before = await client.get(SETTINGS_URL, headers=authorization_for(restaurant_a.admin))

    await configure_billing(client, restaurant_a)
    after = await client.get(SETTINGS_URL, headers=authorization_for(restaurant_a.admin))

    assert before.json()["has_sol_credentials"] is False
    assert before.json()["has_certificate"] is False
    assert before.json()["certificate_expires_at"] is None
    assert before.json()["is_ready"] is False
    body = after.json()
    assert (body["has_sol_credentials"], body["has_certificate"], body["is_ready"]) == (
        True,
        True,
        True,
    )
    assert body["certificate_expires_at"] == "2030-01-01T00:00:00Z"
    for secret in ("sol_user", "sol_password", "certificate", "certificate_pem"):
        assert secret not in body
    assert "MODDATOS" not in after.text
    assert "moddatos" not in after.text
    assert "PRIVATE KEY" not in after.text


async def test_sol_user_and_password_go_together(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.put(
        SETTINGS_URL,
        json=_settings_body(sol_user="MODDATOS"),
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_billing_settings"
    assert response.json()["detail"] == "El usuario y la clave SOL se cambian juntos."


async def test_changing_the_ruc_asks_for_its_sol_credentials_again(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a, certificate=False)

    changed = await client.put(
        SETTINGS_URL,
        json=_settings_body(ruc="20999999999"),
        headers=authorization_for(restaurant_a.admin),
    )
    settings = await client.get(SETTINGS_URL, headers=authorization_for(restaurant_a.admin))

    assert changed.status_code == 422
    assert changed.json()["detail"] == (
        "Si cambias el RUC, vuelve a escribir el usuario y la clave SOL de ese RUC."
    )
    assert settings.json()["ruc"] == "20123456789"


async def test_saving_without_sol_fields_keeps_the_credentials(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)

    kept = await client.put(
        SETTINGS_URL,
        json=_settings_body(legal_name="Doña Rosa SAC"),
        headers=authorization_for(restaurant_a.admin),
    )
    cleared = await client.put(
        SETTINGS_URL,
        json=_settings_body(sol_user="", sol_password=""),
        headers=authorization_for(restaurant_a.admin),
    )

    assert kept.json()["has_sol_credentials"] is True
    assert kept.json()["is_ready"] is True
    assert cleared.json()["has_sol_credentials"] is False
    assert cleared.json()["is_ready"] is False


async def test_credentials_are_registered_in_lycet_only_when_they_change(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    # Sin certificado todavía no hay con qué firmar: nada que registrar.
    await configure_billing(client, restaurant_a, certificate=False)
    assert lycet.registrations() == []
    pfx, pem = make_certificate()

    await client.put(
        CERTIFICATE_URL,
        json=certificate_upload(pfx),
        headers=authorization_for(restaurant_a.admin),
    )
    await client.put(
        SETTINGS_URL,
        json=_settings_body(legal_name="Otro nombre"),
        headers=authorization_for(restaurant_a.admin),
    )

    (registration,) = lycet.registrations()
    method, path, body = registration
    assert (method, path) == ("PUT", "/configuration/company/20123456789")
    assert body["SOL_USER"] == "20123456789MODDATOS"
    assert body["SOL_PASS"] == "moddatos"
    certificate = base64.b64decode(body["certificate"]).decode()
    assert "PRIVATE KEY" in certificate
    assert "BEGIN CERTIFICATE" in certificate
    # La llave que se registra es la del certificado subido.
    assert pem.split(b"-----BEGIN CERTIFICATE-----")[1].decode() in certificate
    assert lycet.tokens == [LYCET_TOKEN]


async def test_new_sol_password_is_registered_again(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)

    await client.put(
        SETTINGS_URL,
        json=_settings_body(sol_user="MODDATOS", sol_password="nueva"),
        headers=authorization_for(restaurant_a.admin),
    )

    passwords = [body["SOL_PASS"] for _, _, body in lycet.registrations()]
    assert passwords == ["moddatos", "nueva"]


async def test_changing_the_ruc_forgets_the_previous_one_in_lycet(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)

    changed = await client.put(
        SETTINGS_URL,
        json=_settings_body(ruc="20999999999", sol_user="OTRO", sol_password="otra"),
        headers=authorization_for(restaurant_a.admin),
    )

    assert changed.status_code == 200, changed.text
    calls = [(method, path) for method, path, _ in lycet.registrations()]
    assert calls == [
        ("PUT", "/configuration/company/20123456789"),
        ("PUT", "/configuration/company/20999999999"),
        ("DELETE", "/configuration/company/20123456789"),
    ]


async def test_lycet_refusing_the_credentials_saves_nothing(
    client: AsyncClient, app: FastAPI, restaurant_a: StaffedRestaurant
) -> None:
    use_fake_lycet(app, FakeLycet(register_status=502))
    await configure_billing(client, restaurant_a, certificate=False)
    pfx, _ = make_certificate()

    response = await client.put(
        CERTIFICATE_URL,
        json=certificate_upload(pfx),
        headers=authorization_for(restaurant_a.admin),
    )
    settings = await client.get(SETTINGS_URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 502
    assert response.json()["code"] == "billing_provider"
    assert settings.json()["has_certificate"] is False


async def test_certificate_pfx_with_its_password_and_pem_without_one(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    pfx, _ = make_certificate(expires_at=datetime(2028, 3, 1, tzinfo=UTC))
    _, pem = make_certificate(expires_at=datetime(2029, 5, 1, tzinfo=UTC))

    from_pfx = await client.put(
        CERTIFICATE_URL,
        json=certificate_upload(pfx),
        headers=authorization_for(restaurant_a.admin),
    )
    from_pem = await client.put(
        CERTIFICATE_URL,
        json=certificate_upload(pem, password=""),
        headers=authorization_for(restaurant_a.admin),
    )

    assert from_pfx.status_code == 200, from_pfx.text
    assert from_pfx.json()["certificate_expires_at"] == "2028-03-01T00:00:00Z"
    assert from_pem.status_code == 200, from_pem.text
    assert from_pem.json()["has_certificate"] is True
    assert from_pem.json()["certificate_expires_at"] == "2029-05-01T00:00:00Z"


@pytest.mark.parametrize(
    "case", ["wrong-password", "not-base64", "too-large", "expired", "not-a-certificate"]
)
async def test_certificate_that_does_not_open_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet, case: str
) -> None:
    pfx, _ = make_certificate()
    expired, _ = make_certificate(expires_at=datetime(2026, 1, 1, tzinfo=UTC))
    body = {
        "wrong-password": certificate_upload(pfx, password="otra"),
        "not-base64": {"content_base64": "@@no es base64@@", "password": ""},
        "too-large": certificate_upload(b"0" * (64 * 1024 + 1)),
        "expired": certificate_upload(expired),
        "not-a-certificate": certificate_upload(b"esto no es un certificado"),
    }[case]

    response = await client.put(
        CERTIFICATE_URL, json=body, headers=authorization_for(restaurant_a.admin)
    )
    settings = await client.get(SETTINGS_URL, headers=authorization_for(restaurant_a.admin))

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_certificate"
    assert "otra" not in response.text
    assert settings.json()["has_certificate"] is False
    assert lycet.registrations() == []


async def test_expired_certificate_says_when_it_expired(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    # El reloj de las pruebas está en el 15 de junio de 2026.
    expired, _ = make_certificate(expires_at=datetime(2026, 6, 1, tzinfo=UTC))

    response = await client.put(
        CERTIFICATE_URL,
        json=certificate_upload(expired),
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.json()["detail"] == "El certificado venció el 2026-06-01: sube uno vigente."


async def test_only_billing_manage_uploads_the_certificate(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    pfx, _ = make_certificate()

    response = await client.put(
        CERTIFICATE_URL,
        json=certificate_upload(pfx),
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 403
    assert lycet.registrations() == []


async def test_activity_log_never_has_the_sol_credentials(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a, sol_user="SECRETO1", sol_password="clave-123")

    activity_log = await client.get(url("activity"), headers=authorization_for(restaurant_a.admin))

    details = [item["detail"] for item in activity_log.json()["items"]]
    assert any("credenciales SOL reemplazadas" in d for d in details)
    assert any("Certificado digital reemplazado (CEVICHERIA DONA ROSA SAC" in d for d in details)
    assert not any("SECRETO1" in d or "clave-123" in d for d in details)


async def test_invalid_tax_data_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.put(
        SETTINGS_URL,
        json={"ruc": "123", "igv_rate": "18", "boleta_series": "B001", "factura_series": "F001"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422


# -- HTTP: emisión ------------------------------------------------------------


async def test_without_credentials_the_invoice_stays_unsent(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    order = await paid_order(client, restaurant_a, menu_a)

    body = await _issue(client, restaurant_a, order["id"])

    assert body["code"] == "B001-1"
    assert body["status"] == "simulated"
    # 56.00 - 10 % = 50.40, con 18 % de IGV incluido.
    assert (body["total"], body["taxable"], body["igv"]) == ("50.40", "42.71", "7.69")
    assert body["discount"] == "5.60"
    assert (body["has_pdf"], body["has_xml"], body["has_cdr"]) == (False, False, False)
    assert lycet.sent() == []


async def test_without_discount_the_invoice_has_the_amount_with_two_decimals(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    # Lo encontró la verificación del contrato con el frontend (Pact): recién
    # emitido, el descuento en cero salía "0" y releído, "0.00".
    order = await paid_order(client, restaurant_a, menu_a, discount=None)

    issued = await _issue(client, restaurant_a, order["id"])
    reread = await client.get(
        f"{BILLING_URL}/orders/{order['id']}/invoice",
        headers=authorization_for(restaurant_a.waiter),
    )

    assert issued["discount"] == reread.json()["discount"] == "0.00"
    assert issued["total"] == "56.00"


async def test_with_credentials_it_is_sent_to_sunat_with_its_documents(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu_a)

    tax_invoice = await client.post(
        f"{BILLING_URL}/invoices",
        json={
            "order_id": order["id"],
            "kind": "factura",
            "customer_document_type": "ruc",
            "customer_document_number": "20987654321",
            "customer_name": "Empresa Cliente S.A.",
        },
        headers=authorization_for(restaurant_a.waiter),
    )
    other = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": order["id"], "kind": "boleta"},
        headers=authorization_for(restaurant_a.waiter),
    )

    body = tax_invoice.json()
    assert tax_invoice.status_code == 201, tax_invoice.text
    assert body["status"] == "accepted"
    assert body["provider_message"].startswith("Aceptado por SUNAT: ")
    assert (body["has_pdf"], body["has_xml"], body["has_cdr"]) == (True, True, True)
    for removed in ("pdf_url", "xml_url", "cdr_url"):
        assert removed not in body
    (sent,) = lycet.sent()
    assert (sent["tipoDoc"], sent["serie"], sent["correlativo"]) == ("01", "F001", "1")
    assert sent["company"]["ruc"] == "20123456789"
    assert sent["client"]["tipoDoc"] == "6"
    assert sent["mtoImpVenta"] == 50.40
    # El token va en la consulta, como lo pide Lycet.
    assert set(lycet.tokens) == {LYCET_TOKEN}
    # Un pedido, un comprobante.
    assert other.status_code == 409


@pytest.mark.parametrize(
    ("reply", "status", "message"),
    [
        ("accepted", "accepted", "Aceptado por SUNAT: La Boleta numero B001-901, ha sido aceptada"),
        ("observed", "accepted", "Aceptado con observaciones: 4312 - El importe total"),
        ("rejected_2800", "rejected", "Rechazado por SUNAT (2800): El dato ingresado"),
        ("rejected_0151", "rejected", "Rechazado por SUNAT (0151): El nombre del archivo ZIP"),
    ],
)
async def test_each_sunat_answer_becomes_a_status_with_its_message(
    client: AsyncClient,
    app: FastAPI,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    reply: str,
    status: str,
    message: str,
) -> None:
    use_fake_lycet(app, FakeLycet(lycet_reply(reply)))
    await configure_billing(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu_a)

    body = await _issue(client, restaurant_a, order["id"])

    assert body["status"] == status
    assert body["provider_message"].startswith(message)
    assert body["has_xml"] is True
    assert body["has_cdr"] is (status == "accepted")
    # El PDF es la representación impresa: solo existe para uno aceptado.
    assert body["has_pdf"] is (status == "accepted")


async def test_lycet_down_leaves_the_invoice_pending(
    client: AsyncClient, app: FastAPI, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    use_fake_lycet(app, FakeLycet({}, status_code=502))
    await configure_billing(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu_a)

    body = await _issue(client, restaurant_a, order["id"])

    assert body["status"] == "pending"
    assert body["provider_message"] == (
        "El servicio de facturación no pudo firmar ni enviar el comprobante (HTTP 502): "
        "revisa el usuario SOL y el certificado del local y reenvíalo."
    )
    assert (body["has_xml"], body["has_cdr"]) == (False, False)


async def test_rejection_is_corrected_and_resent(
    client: AsyncClient, app: FastAPI, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    fake = FakeLycet(lycet_reply("rejected_2800"))
    use_fake_lycet(app, fake)
    await configure_billing(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu_a)
    issued = await _issue(client, restaurant_a, order["id"])
    fake.send = lycet_reply("accepted")

    resent = await client.post(
        f"{BILLING_URL}/invoices/{issued['id']}/resend",
        headers=authorization_for(restaurant_a.admin),
    )
    again = await client.post(
        f"{BILLING_URL}/invoices/{issued['id']}/resend",
        headers=authorization_for(restaurant_a.admin),
    )
    listing = await client.get(
        f"{BILLING_URL}/invoices", headers=authorization_for(restaurant_a.admin)
    )

    assert issued["status"] == "rejected"
    assert "2800" in issued["provider_message"]
    assert resent.json()["status"] == "accepted"
    assert resent.json()["has_cdr"] is True
    assert again.status_code == 422
    assert listing.json()["total"] == 1
    assert len(fake.sent()) == 2


async def test_only_paid_orders_of_the_own_restaurant_are_invoiced(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_a: SeededMenu,
) -> None:
    open_order = await client.post(
        ORDERS_URL,
        json={"type": "takeaway", "items": [{"menu_item_id": menu_a.lomo}]},
        headers=authorization_for(restaurant_a.waiter),
    )
    paid = await paid_order(client, restaurant_a, menu_a)

    unpaid = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": open_order.json()["id"], "kind": "boleta"},
        headers=authorization_for(restaurant_a.waiter),
    )
    foreign_response = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": paid["id"], "kind": "boleta"},
        headers=authorization_for(restaurant_b.admin),
    )
    waiter_settings = await client.get(SETTINGS_URL, headers=authorization_for(restaurant_a.waiter))

    assert unpaid.status_code == 404
    assert foreign_response.status_code == 404
    assert waiter_settings.status_code == 403


async def test_waiter_cannot_issue_or_see_the_invoice_of_a_foreign_order(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
) -> None:
    order = await paid_order(client, restaurant_a, menu_a)
    other_waiter = await staff_with_role(
        session, restaurant_a, "Mesero dos", {Permission.BILLING_ISSUE.value}
    )
    foreign_auth = authorization_for(other_waiter)
    request_body = {"order_id": order["id"], "kind": "boleta"}

    foreign_issue = await client.post(
        f"{BILLING_URL}/invoices", json=request_body, headers=foreign_auth
    )
    issued = await client.post(
        f"{BILLING_URL}/invoices", json=request_body, headers=authorization_for(restaurant_a.waiter)
    )
    foreign_read = await client.get(
        f"{BILLING_URL}/invoices/{issued.json()['id']}", headers=foreign_auth
    )
    foreign_order_invoice = await client.get(
        f"{BILLING_URL}/orders/{order['id']}/invoice", headers=foreign_auth
    )
    owner_read = await client.get(
        f"{BILLING_URL}/invoices/{issued.json()['id']}",
        headers=authorization_for(restaurant_a.admin),
    )
    own_read = await client.get(
        f"{BILLING_URL}/orders/{order['id']}/invoice",
        headers=authorization_for(restaurant_a.waiter),
    )

    assert foreign_issue.status_code == 404
    assert issued.status_code == 201, issued.text
    assert foreign_read.status_code == 404
    assert foreign_order_invoice.status_code == 404
    assert owner_read.status_code == 200
    assert own_read.status_code == 200


async def test_issuing_without_tax_data_numbers_in_sequence_and_does_not_invent_them(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    # Sin datos fiscales, numerar crea la fila por omisión para tener turno.
    first = await paid_order(client, restaurant_a, menu_a)
    second = await paid_order(client, restaurant_a, menu_a)
    for order in (first, second):
        await _issue(client, restaurant_a, order["id"])
    settings = await client.get(SETTINGS_URL, headers=authorization_for(restaurant_a.admin))
    invoice_list = await client.get(
        f"{BILLING_URL}/invoices", headers=authorization_for(restaurant_a.admin)
    )

    assert sorted(i["code"] for i in invoice_list.json()["items"]) == ["B001-1", "B001-2"]
    assert settings.json()["ruc"] == ""
    assert settings.json()["is_ready"] is False
    await configure_billing(client, restaurant_a)


async def test_sandbox_restaurant_never_calls_lycet(
    client: AsyncClient, session: AsyncSession, lycet: FakeLycet
) -> None:
    """Aunque en la vista previa alguien cargue RUC, usuario SOL y certificado, nada sale."""
    sandbox = await staffed_restaurant(session, "muestra-0a1b2c3d", is_sandbox=True)
    menu = await seed_menu(session, sandbox.id)
    await open_cash_session(session, sandbox.id, sandbox.admin.id or 0)
    await configure_billing(client, sandbox)
    order = await paid_order(client, sandbox, menu)

    issued = await _issue(client, sandbox, order["id"])
    resent = await client.post(
        f"{BILLING_URL}/invoices/{issued['id']}/resend",
        headers=authorization_for(sandbox.admin),
    )
    pdf = await client.get(
        f"{BILLING_URL}/invoices/{issued['id']}/pdf", headers=authorization_for(sandbox.admin)
    )

    assert issued["status"] == "simulated"
    assert "Local de muestra" in issued["provider_message"]
    assert resent.json()["status"] == "simulated"
    assert pdf.status_code == 409
    assert lycet.calls == []


# -- HTTP: documentos ----------------------------------------------------------


async def test_documents_are_downloaded_with_their_type_and_name(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu_a)
    issued = await _issue(client, restaurant_a, order["id"])
    reply = lycet_reply("accepted")
    waiter = authorization_for(restaurant_a.waiter)

    pdf = await client.get(f"{BILLING_URL}/invoices/{issued['id']}/pdf", headers=waiter)
    xml = await client.get(f"{BILLING_URL}/invoices/{issued['id']}/xml", headers=waiter)
    cdr = await client.get(f"{BILLING_URL}/invoices/{issued['id']}/cdr", headers=waiter)

    assert (pdf.status_code, pdf.headers["content-type"], pdf.content) == (
        200,
        "application/pdf",
        PDF,
    )
    assert 'filename="B001-1.pdf"' in pdf.headers["content-disposition"]
    assert xml.headers["content-type"].startswith("application/xml")
    assert 'filename="B001-1.xml"' in xml.headers["content-disposition"]
    assert xml.text == reply["xml"]
    assert cdr.headers["content-type"] == "application/zip"
    assert 'filename="R-B001-1.zip"' in cdr.headers["content-disposition"]
    assert cdr.content == base64.b64decode(reply["sunatResponse"]["cdrZip"])
    assert cdr.content.startswith(b"PK")
    # El PDF se arma con el mismo documento que se envió a SUNAT.
    pdf_requests = [body for method, path, body in lycet.calls if path == "/invoice/pdf"]
    assert pdf_requests == lycet.sent()


async def test_unsent_invoice_has_no_documents_to_download(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    order = await paid_order(client, restaurant_a, menu_a)
    issued = await _issue(client, restaurant_a, order["id"])

    responses = [
        await client.get(
            f"{BILLING_URL}/invoices/{issued['id']}/{document}",
            headers=authorization_for(restaurant_a.waiter),
        )
        for document in ("pdf", "xml", "cdr")
    ]

    assert [r.status_code for r in responses] == [409, 409, 409]
    assert {r.json()["code"] for r in responses} == {"invoice_document_unavailable"}
    assert lycet.calls == []


async def test_documents_follow_the_same_visibility_as_the_invoice(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_a: SeededMenu,
    lycet: FakeLycet,
) -> None:
    await configure_billing(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu_a)
    issued = await _issue(client, restaurant_a, order["id"])
    other_waiter = await staff_with_role(
        session, restaurant_a, "Mesero dos", {Permission.BILLING_ISSUE.value}
    )
    document_url = f"{BILLING_URL}/invoices/{issued['id']}/xml"

    foreign_waiter = await client.get(document_url, headers=authorization_for(other_waiter))
    other_restaurant = await client.get(document_url, headers=authorization_for(restaurant_b.admin))
    owner = await client.get(document_url, headers=authorization_for(restaurant_a.admin))
    unknown_format = await client.get(
        f"{BILLING_URL}/invoices/{issued['id']}/zip", headers=authorization_for(restaurant_a.admin)
    )

    assert foreign_waiter.status_code == 404
    assert other_restaurant.status_code == 404
    assert owner.status_code == 200
    assert unknown_format.status_code == 422


async def test_pdf_that_lycet_cannot_render_responds_502(
    client: AsyncClient, app: FastAPI, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    use_fake_lycet(app, FakeLycet(pdf_status=500))
    await configure_billing(client, restaurant_a)
    order = await paid_order(client, restaurant_a, menu_a)
    issued = await _issue(client, restaurant_a, order["id"])

    pdf = await client.get(
        f"{BILLING_URL}/invoices/{issued['id']}/pdf",
        headers=authorization_for(restaurant_a.waiter),
    )

    assert pdf.status_code == 502
    assert pdf.json()["code"] == "billing_provider"
