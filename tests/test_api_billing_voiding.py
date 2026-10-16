"""Anular y reemitir comprobantes por HTTP, con Lycet falso y la base real.

Incluye el índice de «un comprobante vigente por pedido», que es la última
palabra si dos cobros emiten a la vez.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.modules.billing.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyInvoiceRepository,
)
from resthub.modules.billing.domain.exceptions import InvoiceNumberTakenError
from resthub.modules.billing.domain.invoices import Invoice
from tests.builders import SeededMenu, open_cash_session, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url
from tests.fakes import FIXED_NOW
from tests.fakes_lycet import (
    PDF,
    FakeLycet,
    configure_billing,
    lycet_reply,
    paid_order,
    use_fake_lycet,
)

INVOICES_URL = url("billing/invoices")
CUSTOM_CREDIT_SERIES = {"boleta_credit_note_series": "BN01", "factura_credit_note_series": "fn01"}
REASON = {"reason": "El cliente pidió factura"}
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


async def _sales_receipt(
    client: AsyncClient, restaurant: StaffedRestaurant, menu: SeededMenu
) -> dict[str, Any]:
    order = await paid_order(client, restaurant, menu)
    response = await client.post(
        INVOICES_URL,
        json={"order_id": order["id"], "kind": "boleta"},
        headers=authorization_for(restaurant.waiter),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _void(
    client: AsyncClient, restaurant: StaffedRestaurant, invoice_id: int, **body: Any
) -> Any:
    return await client.post(
        f"{INVOICES_URL}/{invoice_id}/void",
        json=body or REASON,
        headers=authorization_for(restaurant.admin),
    )


async def test_void_an_accepted_receipt_and_reissue_it_as_a_tax_invoice(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)
    receipt = await _sales_receipt(client, restaurant_a, menu_a)
    manager = authorization_for(restaurant_a.admin)

    voided = await _void(client, restaurant_a, receipt["id"])

    assert voided.status_code == 200, voided.text
    body = voided.json()
    assert (body["is_voided"], body["void_reason"]) == (True, REASON["reason"])
    assert body["voided_at"] is not None
    note = body["credit_note"]
    assert body["credit_note_id"] == note["id"]
    assert (note["code"], note["status"], note["reason_code"], note["reason_label"]) == (
        "BC01-1",
        "accepted",
        "01",
        "Anulación de la operación",
    )
    assert (note["invoice_id"], note["total"], note["has_pdf"]) == (receipt["id"], "50.40", True)
    (sent,) = lycet.notes_sent()
    assert (sent["serie"], sent["numDocfectado"], sent["tipDocAfectado"]) == (
        "BC01",
        receipt["code"],
        "03",
    )
    # El pedido sigue mostrando el anulado hasta que tenga uno nuevo.
    for_order = url(f"billing/orders/{receipt['order_id']}/invoice")
    assert (await client.get(for_order, headers=manager)).json()["id"] == receipt["id"]

    tax_invoice = await client.post(
        INVOICES_URL, json={"order_id": receipt["order_id"], **TAX_INVOICE_BODY}, headers=manager
    )

    assert tax_invoice.status_code == 201, tax_invoice.text
    assert (tax_invoice.json()["code"], tax_invoice.json()["replaces_invoice_id"]) == (
        "F001-1",
        receipt["id"],
    )
    assert tax_invoice.json()["credit_note"] is None
    assert (await client.get(for_order, headers=manager)).json()["id"] == tax_invoice.json()["id"]
    again = await client.post(
        INVOICES_URL, json={"order_id": receipt["order_id"], **TAX_INVOICE_BODY}, headers=manager
    )
    assert (again.status_code, again.json()["code"]) == (409, "order_already_invoiced")
    listed = await client.get(INVOICES_URL, headers=manager)
    assert [i["credit_note_id"] for i in listed.json()["items"]] == [None, note["id"]]
    activity = await client.get(url("activity"), headers=manager)
    kinds = [entry["kind"] for entry in activity.json()["items"]]
    assert ActivityKind.INVOICE_VOIDED.value in kinds
    assert ActivityKind.CREDIT_NOTE_ISSUED.value in kinds


async def test_rejected_note_keeps_the_invoice_until_resent(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)
    receipt = await _sales_receipt(client, restaurant_a, menu_a)
    lycet.note_send = lycet_reply("rejected_2800")
    manager = authorization_for(restaurant_a.admin)

    response = await _void(client, restaurant_a, receipt["id"])

    assert response.status_code == 200, response.text
    assert response.json()["is_voided"] is False
    note = response.json()["credit_note"]
    assert (note["status"], note["has_pdf"]) == ("rejected", False)
    reissue = await client.post(
        INVOICES_URL, json={"order_id": receipt["order_id"], **TAX_INVOICE_BODY}, headers=manager
    )
    assert reissue.json()["code"] == "order_already_invoiced"
    twice = await _void(client, restaurant_a, receipt["id"])
    assert (twice.status_code, twice.json()["code"]) == (409, "invoice_not_voidable")
    pdf = await client.get(url(f"billing/credit-notes/{note['id']}/pdf"), headers=manager)
    assert (pdf.status_code, pdf.json()["code"]) == (409, "invoice_document_unavailable")

    lycet.note_send = lycet_reply("note_accepted")
    resent = await client.post(url(f"billing/credit-notes/{note['id']}/resend"), headers=manager)

    assert resent.status_code == 200, resent.text
    assert resent.json()["is_voided"] is True
    assert resent.json()["credit_note"]["code"] == "BC01-1"
    assert len(lycet.notes_sent()) == 2
    accepted_again = await client.post(
        url(f"billing/credit-notes/{note['id']}/resend"), headers=manager
    )
    assert (accepted_again.status_code, accepted_again.json()["code"]) == (422, "invalid_invoice")


async def test_unsent_invoice_is_voided_without_a_note(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a, certificate=False)
    receipt = await _sales_receipt(client, restaurant_a, menu_a)
    assert receipt["status"] == "simulated"

    response = await _void(client, restaurant_a, receipt["id"])

    assert response.status_code == 200, response.text
    assert (response.json()["is_voided"], response.json()["credit_note"]) == (True, None)
    assert lycet.notes_sent() == []
    twice = await _void(client, restaurant_a, receipt["id"])
    assert (twice.status_code, twice.json()["code"]) == (409, "invoice_already_voided")
    resend = await client.post(
        f"{INVOICES_URL}/{receipt['id']}/resend", headers=authorization_for(restaurant_a.admin)
    )
    assert (resend.status_code, resend.json()["code"]) == (422, "invalid_invoice")


async def test_pending_invoice_cannot_be_voided_yet(
    app: FastAPI, client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    use_fake_lycet(app, FakeLycet(status_code=502))
    await configure_billing(client, restaurant_a)
    receipt = await _sales_receipt(client, restaurant_a, menu_a)
    assert receipt["status"] == "pending"

    response = await _void(client, restaurant_a, receipt["id"])

    assert (response.status_code, response.json()["code"]) == (409, "invoice_not_voidable")
    assert "pendiente" in response.json()["detail"]


async def test_only_the_manager_voids_and_with_a_reason(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)
    receipt = await _sales_receipt(client, restaurant_a, menu_a)
    endpoint = f"{INVOICES_URL}/{receipt['id']}/void"

    by_waiter = await client.post(
        endpoint, json=REASON, headers=authorization_for(restaurant_a.waiter)
    )
    no_reason = await _void(client, restaurant_a, receipt["id"], reason=" ")
    missing = await _void(client, restaurant_a, 999_999)

    assert by_waiter.status_code == 403
    assert (no_reason.status_code, no_reason.json()["code"]) == (422, "invalid_invoice")
    assert (missing.status_code, missing.json()["code"]) == (404, "invoice_not_found")
    assert lycet.notes_sent() == []


async def test_credit_note_documents_and_other_restaurants(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_a: SeededMenu,
    lycet: FakeLycet,
) -> None:
    await configure_billing(client, restaurant_a)
    receipt = await _sales_receipt(client, restaurant_a, menu_a)
    note = (await _void(client, restaurant_a, receipt["id"])).json()["credit_note"]
    notes = url(f"billing/credit-notes/{note['id']}")
    manager = authorization_for(restaurant_a.admin)

    pdf = await client.get(f"{notes}/pdf", headers=manager)
    xml = await client.get(f"{notes}/xml", headers=authorization_for(restaurant_a.waiter))
    cdr = await client.get(f"{notes}/cdr", headers=manager)
    stranger = await client.get(f"{notes}/xml", headers=authorization_for(restaurant_b.admin))
    stranger_resend = await client.post(
        f"{notes}/resend", headers=authorization_for(restaurant_b.admin)
    )
    stranger_void = await _void(client, restaurant_b, receipt["id"])

    assert (pdf.status_code, pdf.content) == (200, PDF)
    assert "BC01-1.pdf" in pdf.headers["content-disposition"]
    assert lycet.calls[-1][1] == "/note/pdf"
    assert xml.text.startswith("<?xml")
    assert "<CreditNote" in xml.text
    assert "R-BC01-1.zip" in cdr.headers["content-disposition"]
    assert (stranger.status_code, stranger.json()["code"]) == (404, "credit_note_not_found")
    assert stranger_resend.json()["code"] == "credit_note_not_found"
    assert stranger_void.json()["code"] == "invoice_not_found"


async def test_credit_note_series_are_configurable(
    client: AsyncClient, restaurant_a: StaffedRestaurant, lycet: FakeLycet
) -> None:
    settings = url("billing/settings")
    manager = authorization_for(restaurant_a.admin)
    await configure_billing(client, restaurant_a)

    before = (await client.get(settings, headers=manager)).json()
    await configure_billing(
        client,
        restaurant_a,
        certificate=False,
        **CUSTOM_CREDIT_SERIES,
    )
    after = (await client.get(settings, headers=manager)).json()
    # Sin los campos quedan como estaban.
    await configure_billing(client, restaurant_a, certificate=False)
    kept = (await client.get(settings, headers=manager)).json()
    clash = await client.put(
        settings,
        json={
            "ruc": "20123456789",
            "igv_rate": "18.00",
            "boleta_series": "B001",
            "factura_series": "F001",
            "boleta_credit_note_series": "B001",
        },
        headers=manager,
    )

    assert (before["boleta_credit_note_series"], before["factura_credit_note_series"]) == (
        "BC01",
        "FC01",
    )
    assert (after["boleta_credit_note_series"], after["factura_credit_note_series"]) == (
        "BN01",
        "FN01",
    )
    assert kept["boleta_credit_note_series"] == "BN01"
    assert (clash.status_code, clash.json()["code"]) == (422, "invalid_billing_settings")


async def test_voided_invoice_is_not_emailed(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, lycet: FakeLycet
) -> None:
    await configure_billing(client, restaurant_a)
    receipt = await _sales_receipt(client, restaurant_a, menu_a)
    await _void(client, restaurant_a, receipt["id"])

    response = await client.post(
        f"{INVOICES_URL}/{receipt['id']}/email",
        json={"to_email": "ana@correo.pe"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code in {409, 422}


# -- El índice de un comprobante vigente por pedido ---------------------------------------


async def _stored_receipt(
    client: AsyncClient,
    session: AsyncSession,
    restaurant: StaffedRestaurant,
    menu: SeededMenu,
) -> tuple[SqlAlchemyInvoiceRepository, Invoice]:
    await configure_billing(client, restaurant, certificate=False)
    receipt = await _sales_receipt(client, restaurant, menu)
    repository = SqlAlchemyInvoiceRepository(session)
    stored = await repository.get(restaurant.id, receipt["id"])
    assert stored is not None
    return repository, stored


async def test_two_current_invoices_for_one_order_collide(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    lycet: FakeLycet,
) -> None:
    repository, current = await _stored_receipt(client, session, restaurant_a, menu_a)

    with pytest.raises(InvoiceNumberTakenError):
        await repository.add(replace(current, id=None, series="B999", number=1))


async def test_voided_invoices_do_not_count_for_the_index(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    lycet: FakeLycet,
) -> None:
    repository, first = await _stored_receipt(client, session, restaurant_a, menu_a)
    first.void("Cambio de documento", restaurant_a.admin.id or 0, FIXED_NOW)
    await repository.save(first)
    template = replace(first, id=None, series="B999", credit_note=None)

    voided = [await repository.add(replace(template, number=n)) for n in (1, 2)]
    current = await repository.add(
        replace(template, number=3, voided_at=None, void_reason="", voided_by=None)
    )

    assert all(invoice.is_voided for invoice in voided)
    assert await repository.for_order(restaurant_a.id, first.order_id) == current
