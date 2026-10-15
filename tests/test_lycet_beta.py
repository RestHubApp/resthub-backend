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

import secrets
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

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
from tests.fakes import FIXED_NOW


class LycetBeta(BaseSettings):
    """El Lycet local de la prueba: `LYCET_BETA_URL`, `LYCET_BETA_TOKEN` y `LYCET_BETA_CERT`."""

    model_config = SettingsConfigDict(env_prefix="LYCET_BETA_", extra="ignore")

    url: str = ""
    token: str = ""
    # Ruta al PEM de prueba de Greenter (llave privada y certificado).
    cert: str = ""


BETA = LycetBeta()

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
