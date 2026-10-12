"""Comprobantes electrónicos: IGV, reglas de SUNAT y el envío al proveedor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import Permission
from resthub.modules.billing.adapters.api.dependencies import get_invoicer
from resthub.modules.billing.adapters.sunat.nubefact import NubefactInvoicer, build_payload
from resthub.modules.billing.domain.exceptions import InvalidInvoiceError
from resthub.modules.billing.domain.invoices import (
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
)
from tests.builders import SeededMenu, open_cash_session, seed_menu
from tests.conftest import (
    StaffedRestaurant,
    authorization_for,
    staff_with_role,
    staffed_restaurant,
    url,
)
from tests.fakes import FIXED_NOW

BILLING_URL = url("billing")
ORDERS_URL = url("orders")


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

    assert build_payload(billing_settings, sales_receipt)["fecha_de_emision"] == "01-10-2026"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://api.nubefact.com/api/v1/3a1b2c",
        "https://api.nubefact.com:8443/api/v1/abc",
        "https://8.8.8.8/api",
    ],
)
def test_valid_provider_url(url: str) -> None:
    assert provider_url_problem(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://api.nubefact.com/api/v1/abc",
        "ftp://api.nubefact.com/abc",
        "file:///etc/passwd",
        "api.nubefact.com/api/v1/abc",
        "https:///api/v1/abc",
        "https://usuario:clave@api.nubefact.com/api",
        "https://usuario@api.nubefact.com/api",
        "https://api.nubefact.com:99999/api",
        "https://api.nubefact.com/api v1",
        "https://localhost/api",
        "https://127.0.0.1/api",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/api",
        "https://[::1]/api",
        "https://postgres.railway.internal/api",
    ],
)
def test_provider_url_does_not_point_just_anywhere(url: str) -> None:
    """El servidor le manda los comprobantes y el token del local a esa URL."""
    assert provider_url_problem(url) is not None


async def test_nubefact_does_not_call_a_url_saved_before_the_rule() -> None:
    calls: list[httpx.Request] = []
    invoicer = NubefactInvoicer(
        httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    )
    settings = BillingSettings(
        restaurant_id=1,
        ruc="20123456789",
        legal_name="Local",
        provider_url="http://169.254.169.254/latest",
        provider_token="token",
    )
    invoice = Invoice(
        restaurant_id=1,
        order_id=1,
        kind=InvoiceKind.SALES_RECEIPT,
        series="B001",
        number=1,
        customer=Customer(),
        lines=build_lines([("Lomo", 1, Decimal("28.00"), False)]),
        total=Decimal("28.00"),
        discount=Decimal("0.00"),
        igv_rate=Decimal("18.00"),
        issued_by=1,
        issued_at=FIXED_NOW,
    )

    result = await invoicer.send(settings, invoice, InvoiceKind.SALES_RECEIPT)

    assert result.status is InvoiceStatus.REJECTED
    assert calls == []


# -- HTTP ---------------------------------------------------------------------


class FakeNubefact:
    """Un transporte que responde como Nubefact y anota lo que se le mandó."""

    def __init__(self, response: dict[str, Any], status_code: int = 200) -> None:
        self.response = response
        self.status_code = status_code
        self.requests: list[dict[str, Any]] = []
        self.headers: list[str] = []
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers.get("authorization", ""))
        self.urls.append(str(request.url))
        return httpx.Response(self.status_code, json=self.response)


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    menu = await seed_menu(session, restaurant_a.id)
    await open_cash_session(session, restaurant_a.id, restaurant_a.admin.id or 0)
    return menu


def _use_fake(client: AsyncClient, fake: FakeNubefact) -> None:
    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_invoicer] = lambda: NubefactInvoicer(
        httpx.MockTransport(fake.handler)
    )


async def _paid_order(
    client: AsyncClient,
    restaurant: StaffedRestaurant,
    menu: SeededMenu,
    discount: str | None = "10",
) -> dict[str, Any]:
    waiter, owner = authorization_for(restaurant.waiter), authorization_for(restaurant.admin)
    created = await client.post(
        ORDERS_URL,
        json={"type": "takeaway", "items": [{"menu_item_id": menu.lomo, "quantity": 2}]},
        headers=waiter,
    )
    order_id = created.json()["id"]
    for step, actor in (("send", waiter), ("ready", owner), ("served", waiter)):
        await client.post(f"{ORDERS_URL}/{order_id}/{step}", headers=actor)
    if discount is not None:
        await client.put(
            f"{ORDERS_URL}/{order_id}/discount",
            json={"percent": discount, "reason": "Cliente frecuente"},
            headers=waiter,
        )
    paid = await client.post(
        f"{ORDERS_URL}/{order_id}/charge", json={"payment_method": "yape"}, headers=waiter
    )
    assert paid.json()["status"] == "paid", paid.text
    return paid.json()


async def _configure(client: AsyncClient, restaurant: StaffedRestaurant, **extra: Any) -> None:
    body = {
        "ruc": "20123456789",
        "legal_name": "Cevichería Doña Rosa S.A.C.",
        "address": "Av. Larco 123, Trujillo",
        "igv_rate": "18.00",
        "boleta_series": "B001",
        "factura_series": "F001",
        "provider_url": "https://api.nubefact.test/api/v1/abc",
        "provider_token": "token-secreto",
        **extra,
    }
    response = await client.put(
        f"{BILLING_URL}/settings", json=body, headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 200, response.text


async def test_without_provider_the_invoice_stays_unsent(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    order = await _paid_order(client, restaurant_a, menu_a)

    response = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": order["id"], "kind": "boleta"},
        headers=authorization_for(restaurant_a.waiter),
    )

    body = response.json()
    assert response.status_code == 201, response.text
    assert body["code"] == "B001-1"
    assert body["status"] == "simulated"
    # 56.00 - 10 % = 50.40, con 18 % de IGV incluido.
    assert (body["total"], body["taxable"], body["igv"]) == ("50.40", "42.71", "7.69")
    assert body["discount"] == "5.60"


async def test_without_discount_the_invoice_has_the_amount_with_two_decimals(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    # Lo encontró la verificación del contrato con el frontend (Pact): recién
    # emitido, el descuento en cero salía "0" y releído, "0.00".
    order = await _paid_order(client, restaurant_a, menu_a, discount=None)

    issued = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": order["id"], "kind": "boleta"},
        headers=authorization_for(restaurant_a.waiter),
    )
    reread = await client.get(
        f"{BILLING_URL}/orders/{order['id']}/invoice",
        headers=authorization_for(restaurant_a.waiter),
    )

    assert issued.status_code == 201, issued.text
    assert issued.json()["discount"] == reread.json()["discount"] == "0.00"
    assert issued.json()["total"] == "56.00"


async def test_with_provider_it_is_sent_to_sunat_and_the_pdf_is_saved(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    fake = FakeNubefact(
        {
            "aceptada_por_sunat": True,
            "sunat_description": "La Factura numero F001-1, ha sido aceptada",
            "enlace_del_pdf": "https://nubefact.test/f001-1.pdf",
        }
    )
    _use_fake(client, fake)
    await _configure(client, restaurant_a)
    order = await _paid_order(client, restaurant_a, menu_a)

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
    settings = await client.get(
        f"{BILLING_URL}/settings", headers=authorization_for(restaurant_a.admin)
    )

    body = tax_invoice.json()
    assert body["status"] == "accepted"
    assert body["pdf_url"] == "https://nubefact.test/f001-1.pdf"
    sent = fake.requests[0]
    assert (sent["tipo_de_comprobante"], sent["serie"], sent["numero"]) == (1, "F001", 1)
    assert sent["cliente_tipo_de_documento"] == "6"
    assert sent["total"] == "50.40"
    assert fake.headers[0] == 'Token token="token-secreto"'
    # Un pedido, un comprobante.
    assert other.status_code == 409
    # El token nunca vuelve por el API.
    assert "provider_token" not in settings.json()
    assert settings.json()["has_provider_token"] is True


async def test_rejection_is_corrected_and_resent(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    fake = FakeNubefact({"errors": "El RUC del emisor no está habilitado"}, status_code=400)
    _use_fake(client, fake)
    await _configure(client, restaurant_a)
    order = await _paid_order(client, restaurant_a, menu_a)
    issued = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": order["id"], "kind": "boleta"},
        headers=authorization_for(restaurant_a.admin),
    )
    fake.status_code, fake.response = 200, {"aceptada_por_sunat": True}

    resent = await client.post(
        f"{BILLING_URL}/invoices/{issued.json()['id']}/resend",
        headers=authorization_for(restaurant_a.admin),
    )
    again = await client.post(
        f"{BILLING_URL}/invoices/{issued.json()['id']}/resend",
        headers=authorization_for(restaurant_a.admin),
    )
    listing = await client.get(
        f"{BILLING_URL}/invoices", headers=authorization_for(restaurant_a.admin)
    )

    assert issued.json()["status"] == "rejected"
    assert "RUC del emisor" in issued.json()["provider_message"]
    assert resent.json()["status"] == "accepted"
    assert again.status_code == 422
    assert listing.json()["total"] == 1


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
    paid = await _paid_order(client, restaurant_a, menu_a)

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
    waiter_settings = await client.get(
        f"{BILLING_URL}/settings", headers=authorization_for(restaurant_a.waiter)
    )

    assert unpaid.status_code == 404
    assert foreign_response.status_code == 404
    assert waiter_settings.status_code == 403


async def test_waiter_cannot_issue_or_see_the_invoice_of_a_foreign_order(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
) -> None:
    order = await _paid_order(client, restaurant_a, menu_a)
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


async def test_invalid_tax_data_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.put(
        f"{BILLING_URL}/settings",
        json={"ruc": "123", "igv_rate": "18", "boleta_series": "B001", "factura_series": "F001"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422


async def test_issuing_without_tax_data_numbers_in_sequence_and_does_not_invent_them(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    # Sin datos fiscales, numerar crea la fila por omisión para tener turno.
    first = await _paid_order(client, restaurant_a, menu_a)
    second = await _paid_order(client, restaurant_a, menu_a)
    for order in (first, second):
        await client.post(
            f"{BILLING_URL}/invoices",
            json={"order_id": order["id"], "kind": "boleta"},
            headers=authorization_for(restaurant_a.waiter),
        )
    settings = await client.get(
        f"{BILLING_URL}/settings", headers=authorization_for(restaurant_a.admin)
    )
    invoice_list = await client.get(
        f"{BILLING_URL}/invoices", headers=authorization_for(restaurant_a.admin)
    )

    assert sorted(i["code"] for i in invoice_list.json()["items"]) == ["B001-1", "B001-2"]
    assert settings.json()["ruc"] == ""
    assert settings.json()["is_ready"] is False
    await _configure(client, restaurant_a)


@pytest.mark.parametrize(
    "url",
    ["http://api.nubefact.com/api", "https://user:pass@api.nubefact.com/api", "https://10.0.0.1/"],
)
async def test_insecure_provider_url_responds_422(
    client: AsyncClient, restaurant_a: StaffedRestaurant, url: str
) -> None:
    body = {
        "igv_rate": "18.00",
        "boleta_series": "B001",
        "factura_series": "F001",
        "provider_url": url,
    }

    response = await client.put(
        f"{BILLING_URL}/settings", json=body, headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 422
    settings = await client.get(
        f"{BILLING_URL}/settings", headers=authorization_for(restaurant_a.admin)
    )
    assert settings.json()["provider_url"] == ""


async def test_sandbox_restaurant_never_calls_the_provider(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Aunque en la vista previa alguien cargue RUC, URL y token, nada sale del servidor."""
    sandbox = await staffed_restaurant(session, "muestra-0a1b2c3d", is_sandbox=True)
    menu = await seed_menu(session, sandbox.id)
    await open_cash_session(session, sandbox.id, sandbox.admin.id or 0)
    fake = FakeNubefact({"aceptada_por_sunat": True})
    _use_fake(client, fake)
    await _configure(client, sandbox)
    order = await _paid_order(client, sandbox, menu)

    issued = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": order["id"], "kind": "boleta"},
        headers=authorization_for(sandbox.admin),
    )
    resent = await client.post(
        f"{BILLING_URL}/invoices/{issued.json()['id']}/resend",
        headers=authorization_for(sandbox.admin),
    )

    assert issued.status_code == 201, issued.text
    assert issued.json()["status"] == "simulated"
    assert "Local de muestra" in issued.json()["provider_message"]
    assert resent.json()["status"] == "simulated"
    assert fake.requests == []


async def test_changing_provider_url_without_its_token_does_not_walk_away_with_it(
    client: AsyncClient, session: AsyncSession, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    """Un rol con `billing.manage` no apunta la URL a su servidor para recibir el token."""
    fake = FakeNubefact({"errors": "Serie no habilitada"}, status_code=400)
    _use_fake(client, fake)
    await _configure(client, restaurant_a)
    order = await _paid_order(client, restaurant_a, menu_a)
    rejected = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": order["id"], "kind": "boleta"},
        headers=authorization_for(restaurant_a.waiter),
    )
    accountant = authorization_for(
        await staff_with_role(session, restaurant_a, "Contador", {Permission.BILLING_MANAGE.value})
    )
    body = {
        "ruc": "20123456789",
        "legal_name": "Cevichería Doña Rosa S.A.C.",
        "igv_rate": "18.00",
        "boleta_series": "B001",
        "factura_series": "F001",
        "provider_url": "https://atacante.example/captura",
    }

    change = await client.put(f"{BILLING_URL}/settings", json=body, headers=accountant)
    resend = await client.post(
        f"{BILLING_URL}/invoices/{rejected.json()['id']}/resend", headers=accountant
    )
    settings = await client.get(f"{BILLING_URL}/settings", headers=accountant)

    assert rejected.json()["status"] == "rejected"
    assert change.status_code == 422
    assert change.json()["detail"] == (
        "Si cambias la ruta del proveedor, vuelve a escribir su token."
    )
    assert settings.json()["provider_url"] == "https://api.nubefact.test/api/v1/abc"
    assert resend.status_code == 200
    assert all(url.startswith("https://api.nubefact.test/") for url in fake.urls)
    assert len(fake.urls) == 2


async def test_activity_log_records_provider_server_and_token_change(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await _configure(client, restaurant_a)
    await _configure(
        client,
        restaurant_a,
        provider_url="https://api.nubefact.com/api/v1/otra",
        provider_token="t2",
    )

    activity_log = await client.get(url("activity"), headers=authorization_for(restaurant_a.admin))

    details = [item["detail"] for item in activity_log.json()["items"]]
    assert any(
        "token del proveedor reemplazado; proveedor en api.nubefact.com" in d for d in details
    )
    assert not any("t2" in d or url("otra") in d for d in details)
