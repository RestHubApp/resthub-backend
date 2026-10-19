"""El régimen tributario del local decide qué comprobantes emite (Nuevo RUS: solo boletas)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.billing.domain.exceptions import InvalidInvoiceError
from resthub.modules.billing.domain.invoices import BillingSettings, InvoiceKind, TaxRegime
from tests.builders import SeededMenu, open_cash_session, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url
from tests.fakes_lycet import FakeLycet, configure_billing, paid_order, use_fake_lycet

SETTINGS_URL = url("billing/settings")
INVOICES_URL = url("billing/invoices")
TAX_INVOICE_BODY = {
    "kind": "factura",
    "customer_document_type": "ruc",
    "customer_document_number": "20100066603",
    "customer_name": "Cliente de prueba S.A.C.",
}


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


def test_only_the_new_rus_cannot_issue_tax_invoices() -> None:
    allowed = {regime for regime in TaxRegime if regime.allows_tax_invoice}

    assert allowed == {TaxRegime.GENERAL, TaxRegime.SPECIAL, TaxRegime.MYPE}
    assert all(regime.label for regime in TaxRegime)


def test_settings_start_in_the_general_regime_and_check_the_kind() -> None:
    assert BillingSettings(restaurant_id=1).tax_regime is TaxRegime.GENERAL
    rus = BillingSettings(restaurant_id=1, tax_regime=TaxRegime.NEW_RUS)

    rus.ensure_can_issue(InvoiceKind.SALES_RECEIPT)
    BillingSettings(restaurant_id=1).ensure_can_issue(InvoiceKind.TAX_INVOICE)
    with pytest.raises(InvalidInvoiceError, match="solo emite boletas"):
        rus.ensure_can_issue(InvoiceKind.TAX_INVOICE)


async def test_regime_is_configured_and_kept_when_not_sent(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    manager = authorization_for(restaurant_a.admin)
    await configure_billing(client, restaurant_a)

    first = (await client.get(SETTINGS_URL, headers=manager)).json()
    await configure_billing(client, restaurant_a, certificate=False, tax_regime="new_rus")
    rus = (await client.get(SETTINGS_URL, headers=manager)).json()
    await configure_billing(client, restaurant_a, certificate=False)
    kept = (await client.get(SETTINGS_URL, headers=manager)).json()

    assert (first["tax_regime"], first["allows_tax_invoice"]) == ("general", True)
    assert (rus["tax_regime"], rus["allows_tax_invoice"]) == ("new_rus", False)
    assert (kept["tax_regime"], kept["allows_tax_invoice"]) == ("new_rus", False)


async def test_capabilities_tell_issuers_whether_tax_invoices_are_allowed(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    waiter = authorization_for(restaurant_a.waiter)
    capabilities = url("billing/capabilities")
    await configure_billing(client, restaurant_a, tax_regime="new_rus")

    rus = (await client.get(capabilities, headers=waiter)).json()
    await configure_billing(client, restaurant_a, certificate=False, tax_regime="general")
    general = (await client.get(capabilities, headers=waiter)).json()

    assert (rus["tax_invoice_enabled"], general["tax_invoice_enabled"]) == (False, True)


async def test_unknown_regime_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.put(
        SETTINGS_URL,
        json={
            "igv_rate": "18.00",
            "boleta_series": "B001",
            "factura_series": "F001",
            "tax_regime": "otro",
        },
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422


async def test_new_rus_issues_receipts_but_not_tax_invoices(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    lycet: FakeLycet,
) -> None:
    await configure_billing(client, restaurant_a, tax_regime="new_rus")
    waiter = authorization_for(restaurant_a.waiter)
    order = await paid_order(client, restaurant_a, menu_a)

    refused = await client.post(
        INVOICES_URL, json={"order_id": order["id"], **TAX_INVOICE_BODY}, headers=waiter
    )
    receipt = await client.post(
        INVOICES_URL, json={"order_id": order["id"], "kind": "boleta"}, headers=waiter
    )

    assert (refused.status_code, refused.json()["code"]) == (422, "invalid_invoice")
    assert "solo emite boletas" in refused.json()["detail"]
    assert receipt.status_code == 201, receipt.text


async def test_changing_the_regime_enables_tax_invoices(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    lycet: FakeLycet,
) -> None:
    await configure_billing(client, restaurant_a, tax_regime="new_rus")
    await configure_billing(client, restaurant_a, certificate=False, tax_regime="mype")
    order = await paid_order(client, restaurant_a, menu_a)

    response = await client.post(
        INVOICES_URL,
        json={"order_id": order["id"], **TAX_INVOICE_BODY},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 201, response.text


async def test_waiter_cannot_change_the_regime(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.put(
        SETTINGS_URL,
        json={
            "igv_rate": "18.00",
            "boleta_series": "B001",
            "factura_series": "F001",
            "tax_regime": "mype",
        },
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 403
