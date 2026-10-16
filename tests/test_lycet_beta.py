r"""Integración real: el adaptador contra un Lycet local que envía a SUNAT BETA.

Se omite salvo que se pida. Para correrla, con Lycet levantado
(`docker compose -f docker/lycet/compose.yml up -d --build`):

    docker compose -f docker/lycet/compose.yml exec lycet cat tests/Resources/cert.pem \
        > cert-beta.pem
    LYCET_BETA_URL=http://127.0.0.1:8088 LYCET_BETA_TOKEN=resthub-local \
    LYCET_BETA_CERT=cert-beta.pem pytest tests/test_lycet_beta.py -q

Usa las credenciales de demostración de Greenter (RUC 20000000001, usuario SOL
MODDATOS) y su certificado de prueba público. Nunca producción.
"""

from __future__ import annotations

import asyncio
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.billing.adapters.api.dependencies import get_invoicer
from resthub.modules.billing.adapters.sunat.certificates import CryptographyCertificateReader
from resthub.modules.billing.adapters.sunat.lycet import LycetGateway
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    build_lines,
)
from tests.builders import open_cash_session, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url
from tests.fakes import FIXED_NOW
from tests.fakes_lycet import certificate_upload, configure_billing, paid_order


class LycetBeta(BaseSettings):
    """El Lycet local de la prueba: `LYCET_BETA_URL`, `LYCET_BETA_TOKEN` y `LYCET_BETA_CERT`."""

    model_config = SettingsConfigDict(env_prefix="LYCET_BETA_", extra="ignore")

    url: str = ""
    token: str = ""
    # Ruta al PEM de prueba de Greenter (llave privada y certificado).
    cert: str = ""


BETA = LycetBeta()
# SUNAT beta responde «Unauthorized» de vez en cuando con las credenciales de demostración.
_BETA_RETRIES = 5

pytestmark = [
    pytest.mark.skipif(
        not (BETA.url and BETA.token and BETA.cert),
        reason="Sin LYCET_BETA_URL, LYCET_BETA_TOKEN y LYCET_BETA_CERT: no hay Lycet local.",
    ),
    # El certificado de prueba de Greenter tiene un número de serie negativo.
    pytest.mark.filterwarnings("ignore::cryptography.utils.CryptographyDeprecationWarning"),
]


@pytest.fixture
async def beta() -> tuple[LycetGateway, BillingSettings]:
    pem = Path(BETA.cert).read_bytes()  # noqa: ASYNC240 - un archivo chico, solo en esta prueba
    parsed = CryptographyCertificateReader().read(pem, "")
    settings = BillingSettings(
        restaurant_id=1,
        ruc="20000000001",
        legal_name="RESTHUB PRUEBAS S.A.C.",
        address="Av. Larco 123, Trujillo",
        sol_user="MODDATOS",
        # Credencial pública de demostración de SUNAT.
        sol_password="moddatos",
        certificate_pem=parsed.pem,
    )
    gateway = LycetGateway(BETA.url, BETA.token)
    await gateway.register(settings)
    return gateway, settings


@pytest.mark.parametrize(
    ("kind", "series", "customer"),
    [
        (InvoiceKind.SALES_RECEIPT, "B001", Customer()),
        (
            InvoiceKind.TAX_INVOICE,
            "F001",
            Customer(DocumentType.RUC, "20100066603", "CLIENTE DE PRUEBA S.A.C.", "Av. Lima 1"),
        ),
    ],
    ids=["boleta", "factura"],
)
async def test_sunat_beta_accepts_a_receipt_with_discount_and_courtesy(
    beta: tuple[LycetGateway, BillingSettings],
    kind: InvoiceKind,
    series: str,
    customer: Customer,
) -> None:
    gateway, settings = beta
    lines = build_lines(
        [
            ("Lomo saltado", 2, Decimal("32.50"), False),
            ("Chicha morada 1L", 1, Decimal("12.00"), False),
            ("Postre de cortesía", 1, Decimal("9.90"), True),
        ]
    )
    invoice = Invoice(
        restaurant_id=1,
        order_id=1,
        kind=kind,
        series=series,
        # Beta no guarda el correlativo, pero uno al azar evita confundir corridas en los logs.
        number=secrets.randbelow(90_000_000) + 10_000_000,
        customer=customer,
        lines=lines,
        total=Decimal("70.00"),
        discount=Decimal("7.00"),
        igv_rate=Decimal("18.00"),
        issued_by=1,
        issued_at=FIXED_NOW,
    )

    result = await gateway.send(settings, invoice, kind)

    assert result.status is InvoiceStatus.ACCEPTED, result.message
    assert result.signed_xml.startswith("<?xml")
    assert result.cdr_zip
    invoice.document_payload = result.document_payload
    assert (await gateway.render_pdf(invoice)).startswith(b"%PDF")


async def test_sunat_beta_voids_an_accepted_receipt_and_reissues_it_as_a_tax_invoice(
    app: FastAPI,
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    session: AsyncSession,
) -> None:
    """De punta a punta por el API: boleta aceptada, nota de crédito aceptada, factura nueva."""
    app.dependency_overrides[get_invoicer] = lambda: LycetGateway(BETA.url, BETA.token)
    menu = await seed_menu(session, restaurant_a.id)
    await open_cash_session(session, restaurant_a.id, restaurant_a.admin.id or 0)
    await configure_billing(
        client,
        restaurant_a,
        certificate=False,
        ruc="20000000001",
        legal_name="RESTHUB PRUEBAS S.A.C.",
    )
    manager = authorization_for(restaurant_a.admin)
    pem = Path(BETA.cert).read_bytes()  # noqa: ASYNC240 - un archivo chico, solo en esta prueba
    uploaded = await client.put(
        url("billing/settings/certificate"), json=certificate_upload(pem, ""), headers=manager
    )
    assert uploaded.status_code == 200, uploaded.text
    order = await paid_order(client, restaurant_a, menu)
    invoices = url("billing/invoices")

    async def accepted(response: Response, resend: str) -> dict[str, Any]:
        """Beta a veces responde «Unauthorized»: lo pendiente se reenvía, como el encargado."""
        body: dict[str, Any] = response.json()
        for _ in range(_BETA_RETRIES):
            document = body.get("credit_note") or body
            if document["status"] != "pending":
                break
            await asyncio.sleep(2)
            path = resend.format(id=document["id"])
            body = (await client.post(path, headers=manager)).json()
        return body

    receipt = await accepted(
        await client.post(
            invoices, json={"order_id": order["id"], "kind": "boleta"}, headers=manager
        ),
        f"{invoices}/{{id}}/resend",
    )
    assert receipt["status"] == "accepted", receipt
    voided = await accepted(
        await client.post(
            f"{invoices}/{receipt['id']}/void",
            json={"reason": "El cliente pidió factura"},
            headers=manager,
        ),
        url("billing/credit-notes/{id}/resend"),
    )
    note = voided["credit_note"]
    assert note["status"] == "accepted", voided
    assert voided["is_voided"] is True
    tax_invoice = await accepted(
        await client.post(
            invoices,
            json={
                "order_id": order["id"],
                "kind": "factura",
                "customer_document_type": "ruc",
                "customer_document_number": "20100066603",
                "customer_name": "CLIENTE DE PRUEBA S.A.C.",
            },
            headers=manager,
        ),
        f"{invoices}/{{id}}/resend",
    )

    assert tax_invoice["status"] == "accepted", tax_invoice
    assert tax_invoice["replaces_invoice_id"] == receipt["id"]
    pdf = await client.get(url(f"billing/credit-notes/{note['id']}/pdf"), headers=manager)
    assert pdf.content.startswith(b"%PDF")
    print(  # noqa: T201 - los números de la corrida, para el informe
        f"\nboleta {receipt['code']} -> nota {note['code']} ({note['provider_message']}) "
        f"-> factura {tax_invoice['code']} ({tax_invoice['provider_message']})"
    )
