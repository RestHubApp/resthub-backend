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

from resthub.modules.billing.adapters.api.router import get_invoicer
from resthub.modules.billing.adapters.sunat.nubefact import NubefactInvoicer, build_payload
from resthub.modules.billing.domain.exceptions import InvalidInvoice
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
from tests.builders import Carta, caja_abierta, carta
from tests.conftest import StaffedRestaurant, authorization_for, staffed_restaurant

BILLING_URL = "/api/v1/billing"
ORDERS_URL = "/api/v1/orders"


# -- Dominio ------------------------------------------------------------------


def test_el_igv_se_separa_de_un_precio_que_ya_lo_incluye() -> None:
    assert split_igv(Decimal("118.00"), Decimal("18")) == (Decimal("100.00"), Decimal("18.00"))
    assert split_igv(Decimal("61.50"), Decimal("10.5")) == (Decimal("55.66"), Decimal("5.84"))


@pytest.mark.parametrize(
    ("kind", "customer", "total"),
    [
        (InvoiceKind.FACTURA, Customer(DocumentType.DNI, "12345678", "Ana"), "50"),
        (InvoiceKind.FACTURA, Customer(DocumentType.RUC, "20123456789", ""), "50"),
        (InvoiceKind.BOLETA, Customer(), "700.01"),
        (InvoiceKind.BOLETA, Customer(DocumentType.DNI, "1234", "Ana"), "50"),
    ],
    ids=["factura-sin-ruc", "factura-sin-razon-social", "boleta-anonima-grande", "dni-corto"],
)
def test_las_reglas_de_sunat_sobre_el_cliente(
    kind: InvoiceKind, customer: Customer, total: str
) -> None:
    with pytest.raises(InvalidInvoice):
        validate_customer(kind, customer, Decimal(total))


def test_una_boleta_chica_puede_ir_a_clientes_varios() -> None:
    cliente = validate_customer(InvoiceKind.BOLETA, Customer(), Decimal("699.99"))

    assert cliente.name == "Clientes varios"


def test_la_fecha_de_emision_es_la_del_dia_del_local() -> None:
    # 21:30 del 1 de octubre en Lima ya son las 02:30 del 2 en UTC.
    boleta = Invoice(
        restaurant_id=1,
        order_id=1,
        kind=InvoiceKind.BOLETA,
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
    local = BillingSettings(restaurant_id=1, timezone="America/Lima")

    assert build_payload(local, boleta)["fecha_de_emision"] == "01-10-2026"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://api.nubefact.com/api/v1/3a1b2c",
        "https://api.nubefact.com:8443/api/v1/abc",
        "https://8.8.8.8/api",
    ],
)
def test_la_url_del_proveedor_que_sirve(url: str) -> None:
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
def test_la_url_del_proveedor_no_apunta_a_cualquier_lado(url: str) -> None:
    """El servidor le manda los comprobantes y el token del local a esa URL."""
    assert provider_url_problem(url) is not None


async def test_nubefact_no_llama_a_una_url_guardada_antes_de_la_regla() -> None:
    llamadas: list[httpx.Request] = []
    invoicer = NubefactInvoicer(
        httpx.MockTransport(lambda r: llamadas.append(r) or httpx.Response(200))
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
        kind=InvoiceKind.BOLETA,
        series="B001",
        number=1,
        customer=Customer(),
        lines=build_lines([("Lomo", 1, Decimal("28.00"), False)]),
        total=Decimal("28.00"),
        discount=Decimal("0.00"),
        igv_rate=Decimal("18.00"),
        issued_by=1,
        issued_at=datetime.now(UTC),
    )

    result = await invoicer.send(settings, invoice, InvoiceKind.BOLETA)

    assert result.status is InvoiceStatus.REJECTED
    assert llamadas == []


# -- HTTP ---------------------------------------------------------------------


class FakeNubefact:
    """Un transporte que responde como Nubefact y anota lo que se le mandó."""

    def __init__(self, response: dict[str, Any], status_code: int = 200) -> None:
        self.response = response
        self.status_code = status_code
        self.requests: list[dict[str, Any]] = []
        self.headers: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers.get("authorization", ""))
        return httpx.Response(self.status_code, json=self.response)


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    menu = await carta(session, local_a.id)
    await caja_abierta(session, local_a.id, local_a.admin.id or 0)
    return menu


def _usar(client: AsyncClient, fake: FakeNubefact) -> None:
    app = client._transport.app  # type: ignore[attr-defined]  # noqa: SLF001
    app.dependency_overrides[get_invoicer] = lambda: NubefactInvoicer(
        httpx.MockTransport(fake.handler)
    )


async def _pagado(
    client: AsyncClient, local: StaffedRestaurant, menu: Carta, descuento: str | None = "10"
) -> dict[str, Any]:
    mesero, encargado = authorization_for(local.waiter), authorization_for(local.admin)
    creado = await client.post(
        ORDERS_URL,
        json={"type": "takeaway", "items": [{"menu_item_id": menu.lomo, "quantity": 2}]},
        headers=mesero,
    )
    order_id = creado.json()["id"]
    for paso, quien in (("send", mesero), ("ready", encargado), ("served", mesero)):
        await client.post(f"{ORDERS_URL}/{order_id}/{paso}", headers=quien)
    if descuento is not None:
        await client.put(
            f"{ORDERS_URL}/{order_id}/discount",
            json={"percent": descuento, "reason": "Cliente frecuente"},
            headers=mesero,
        )
    pagado = await client.post(
        f"{ORDERS_URL}/{order_id}/charge", json={"payment_method": "yape"}, headers=mesero
    )
    assert pagado.json()["status"] == "paid", pagado.text
    return pagado.json()


async def _configurar(client: AsyncClient, local: StaffedRestaurant, **extra: Any) -> None:
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
        f"{BILLING_URL}/settings", json=body, headers=authorization_for(local.admin)
    )
    assert response.status_code == 200, response.text


async def test_sin_proveedor_el_comprobante_queda_sin_enviar(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    pedido = await _pagado(client, local_a, carta_a)

    response = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": pedido["id"], "kind": "boleta"},
        headers=authorization_for(local_a.waiter),
    )

    body = response.json()
    assert response.status_code == 201, response.text
    assert body["code"] == "B001-1"
    assert body["status"] == "simulated"
    # 56.00 − 10 % = 50.40, con 18 % de IGV incluido.
    assert (body["total"], body["taxable"], body["igv"]) == ("50.40", "42.71", "7.69")
    assert body["discount"] == "5.60"


async def test_sin_descuento_el_comprobante_trae_el_importe_con_dos_decimales(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    # Lo encontró la verificación del contrato con el frontend (Pact): recién
    # emitido, el descuento en cero salía "0" y releído, "0.00".
    pedido = await _pagado(client, local_a, carta_a, descuento=None)

    emitido = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": pedido["id"], "kind": "boleta"},
        headers=authorization_for(local_a.waiter),
    )
    releido = await client.get(
        f"{BILLING_URL}/orders/{pedido['id']}/invoice", headers=authorization_for(local_a.waiter)
    )

    assert emitido.status_code == 201, emitido.text
    assert emitido.json()["discount"] == releido.json()["discount"] == "0.00"
    assert emitido.json()["total"] == "56.00"


async def test_con_proveedor_se_envia_a_sunat_y_guarda_el_pdf(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    fake = FakeNubefact(
        {
            "aceptada_por_sunat": True,
            "sunat_description": "La Factura numero F001-1, ha sido aceptada",
            "enlace_del_pdf": "https://nubefact.test/f001-1.pdf",
        }
    )
    _usar(client, fake)
    await _configurar(client, local_a)
    pedido = await _pagado(client, local_a, carta_a)

    factura = await client.post(
        f"{BILLING_URL}/invoices",
        json={
            "order_id": pedido["id"],
            "kind": "factura",
            "customer_document_type": "ruc",
            "customer_document_number": "20987654321",
            "customer_name": "Empresa Cliente S.A.",
        },
        headers=authorization_for(local_a.waiter),
    )
    otra = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": pedido["id"], "kind": "boleta"},
        headers=authorization_for(local_a.waiter),
    )
    ajustes = await client.get(f"{BILLING_URL}/settings", headers=authorization_for(local_a.admin))

    body = factura.json()
    assert body["status"] == "accepted"
    assert body["pdf_url"] == "https://nubefact.test/f001-1.pdf"
    enviado = fake.requests[0]
    assert (enviado["tipo_de_comprobante"], enviado["serie"], enviado["numero"]) == (1, "F001", 1)
    assert enviado["cliente_tipo_de_documento"] == "6"
    assert enviado["total"] == "50.40"
    assert fake.headers[0] == 'Token token="token-secreto"'
    # Un pedido, un comprobante.
    assert otra.status_code == 409
    # El token nunca vuelve por el API.
    assert "provider_token" not in ajustes.json()
    assert ajustes.json()["has_provider_token"] is True


async def test_un_rechazo_se_corrige_y_se_reenvia(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    fake = FakeNubefact({"errors": "El RUC del emisor no está habilitado"}, status_code=400)
    _usar(client, fake)
    await _configurar(client, local_a)
    pedido = await _pagado(client, local_a, carta_a)
    emitido = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": pedido["id"], "kind": "boleta"},
        headers=authorization_for(local_a.admin),
    )
    fake.status_code, fake.response = 200, {"aceptada_por_sunat": True}

    reenviado = await client.post(
        f"{BILLING_URL}/invoices/{emitido.json()['id']}/resend",
        headers=authorization_for(local_a.admin),
    )
    otra_vez = await client.post(
        f"{BILLING_URL}/invoices/{emitido.json()['id']}/resend",
        headers=authorization_for(local_a.admin),
    )
    listado = await client.get(f"{BILLING_URL}/invoices", headers=authorization_for(local_a.admin))

    assert emitido.json()["status"] == "rejected"
    assert "RUC del emisor" in emitido.json()["provider_message"]
    assert reenviado.json()["status"] == "accepted"
    assert otra_vez.status_code == 422
    assert listado.json()["total"] == 1


async def test_solo_se_emite_lo_pagado_y_del_propio_local(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    carta_a: Carta,
) -> None:
    abierto = await client.post(
        ORDERS_URL,
        json={"type": "takeaway", "items": [{"menu_item_id": carta_a.lomo}]},
        headers=authorization_for(local_a.waiter),
    )
    pagado = await _pagado(client, local_a, carta_a)

    sin_pagar = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": abierto.json()["id"], "kind": "boleta"},
        headers=authorization_for(local_a.waiter),
    )
    ajeno = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": pagado["id"], "kind": "boleta"},
        headers=authorization_for(local_b.admin),
    )
    ajustes_del_mesero = await client.get(
        f"{BILLING_URL}/settings", headers=authorization_for(local_a.waiter)
    )

    assert sin_pagar.status_code == 404
    assert ajeno.status_code == 404
    assert ajustes_del_mesero.status_code == 403


async def test_datos_fiscales_invalidos_se_rechazan(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.put(
        f"{BILLING_URL}/settings",
        json={"ruc": "123", "igv_rate": "18", "boleta_series": "B001", "factura_series": "F001"},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 422


async def test_emitir_sin_datos_fiscales_numera_en_serie_y_no_los_inventa(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    # Sin datos fiscales, numerar crea la fila por omisión para tener turno.
    uno = await _pagado(client, local_a, carta_a)
    dos = await _pagado(client, local_a, carta_a)
    for pedido in (uno, dos):
        await client.post(
            f"{BILLING_URL}/invoices",
            json={"order_id": pedido["id"], "kind": "boleta"},
            headers=authorization_for(local_a.waiter),
        )
    ajustes = await client.get(f"{BILLING_URL}/settings", headers=authorization_for(local_a.admin))
    lista = await client.get(f"{BILLING_URL}/invoices", headers=authorization_for(local_a.admin))

    assert sorted(i["code"] for i in lista.json()["items"]) == ["B001-1", "B001-2"]
    assert ajustes.json()["ruc"] == ""
    assert ajustes.json()["is_ready"] is False
    await _configurar(client, local_a)


@pytest.mark.parametrize(
    "url",
    ["http://api.nubefact.com/api", "https://user:pass@api.nubefact.com/api", "https://10.0.0.1/"],
)
async def test_una_url_de_proveedor_insegura_responde_422(
    client: AsyncClient, local_a: StaffedRestaurant, url: str
) -> None:
    body = {
        "igv_rate": "18.00",
        "boleta_series": "B001",
        "factura_series": "F001",
        "provider_url": url,
    }

    response = await client.put(
        f"{BILLING_URL}/settings", json=body, headers=authorization_for(local_a.admin)
    )

    assert response.status_code == 422
    ajustes = await client.get(f"{BILLING_URL}/settings", headers=authorization_for(local_a.admin))
    assert ajustes.json()["provider_url"] == ""


async def test_el_local_de_muestra_nunca_llama_al_proveedor(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Aunque en la vista previa alguien cargue RUC, URL y token, nada sale del servidor."""
    muestra = await staffed_restaurant(session, "muestra-0a1b2c3d", is_sandbox=True)
    menu = await carta(session, muestra.id)
    await caja_abierta(session, muestra.id, muestra.admin.id or 0)
    fake = FakeNubefact({"aceptada_por_sunat": True})
    _usar(client, fake)
    await _configurar(client, muestra)
    pedido = await _pagado(client, muestra, menu)

    emitido = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": pedido["id"], "kind": "boleta"},
        headers=authorization_for(muestra.admin),
    )
    reenviado = await client.post(
        f"{BILLING_URL}/invoices/{emitido.json()['id']}/resend",
        headers=authorization_for(muestra.admin),
    )

    assert emitido.status_code == 201, emitido.text
    assert emitido.json()["status"] == "simulated"
    assert "Local de muestra" in emitido.json()["provider_message"]
    assert reenviado.json()["status"] == "simulated"
    assert fake.requests == []
