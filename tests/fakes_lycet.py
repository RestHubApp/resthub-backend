"""Dobles de Lycet y certificados de prueba: ninguna prueba unitaria llama a Lycet ni a SUNAT.

Las respuestas de `tests/fixtures/lycet/` se grabaron de un Lycet local contra el
entorno BETA de SUNAT (credenciales de demostración de Greenter).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from fastapi import FastAPI
from httpx import AsyncClient

from resthub.modules.billing.adapters.api.dependencies import get_invoicer
from resthub.modules.billing.adapters.sunat.lycet import API_PATH, LycetGateway
from tests.builders import SeededMenu
from tests.conftest import StaffedRestaurant, authorization_for, url

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lycet"
LYCET_URL = "https://lycet.test"
LYCET_TOKEN = "token-de-lycet"
PDF = b"%PDF-1.4 comprobante de prueba"
PFX_PASSWORD = "clave-del-pfx"


def lycet_reply(name: str) -> dict[str, Any]:
    """Una respuesta grabada: `accepted`, `observed`, `rejected_2800` o `rejected_0151`."""
    return json.loads((FIXTURES / f"send_{name}.json").read_text(encoding="utf-8"))


class FakeLycet:
    """Un transporte que responde como Lycet y anota lo que se le pidió."""

    def __init__(
        self,
        send: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
        register_status: int = 200,
        pdf_status: int = 200,
    ) -> None:
        self.send = lycet_reply("accepted") if send is None else send
        self.status_code = status_code
        self.register_status = register_status
        self.pdf_status = pdf_status
        # (método, ruta sin el prefijo del API de Lycet, cuerpo JSON o None)
        self.calls: list[tuple[str, str, Any]] = []
        self.tokens: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(API_PATH)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        self.tokens.append(request.url.params.get("token", ""))
        if path.startswith("/configuration/company/"):
            ruc = path.rsplit("/", 1)[-1]
            return httpx.Response(self.register_status, json={"ruc": ruc, "message": "ok"})
        if path == "/invoice/pdf":
            return httpx.Response(
                self.pdf_status, content=PDF, headers={"content-type": "application/pdf"}
            )
        return httpx.Response(self.status_code, json=self.send)

    def sent(self) -> list[dict[str, Any]]:
        """Los comprobantes enviados a SUNAT, en orden."""
        return [body for method, path, body in self.calls if path == "/invoice/send"]

    def registrations(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[1].startswith("/configuration/company/")]

    def gateway(self) -> LycetGateway:
        return LycetGateway(LYCET_URL, LYCET_TOKEN, httpx.MockTransport(self.handler))


def use_fake_lycet(app: FastAPI, fake: FakeLycet) -> None:
    app.dependency_overrides[get_invoicer] = fake.gateway


def make_certificate(
    *,
    password: str = PFX_PASSWORD,
    expires_at: datetime | None = None,
    common_name: str = "CEVICHERIA DONA ROSA SAC",
) -> tuple[bytes, bytes]:
    """Un certificado RSA autofirmado de prueba: (el .pfx con su contraseña, el .pem sin ella)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name)])
    not_after = expires_at or datetime(2030, 1, 1, tzinfo=UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_after - timedelta(days=3650))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    pfx = pkcs12.serialize_key_and_certificates(
        b"prueba",
        key,
        certificate,
        None,
        serialization.BestAvailableEncryption(password.encode()),
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ) + certificate.public_bytes(serialization.Encoding.PEM)
    return pfx, pem


def certificate_upload(content: bytes, password: str = PFX_PASSWORD) -> dict[str, str]:
    """El cuerpo de `PUT /billing/settings/certificate`."""
    return {"content_base64": base64.b64encode(content).decode("ascii"), "password": password}


async def configure_billing(
    client: AsyncClient,
    restaurant: StaffedRestaurant,
    *,
    certificate: bool = True,
    **extra: Any,
) -> None:
    """Deja al local listo para emitir: datos fiscales, usuario SOL y (si se pide) certificado."""
    body = {
        "ruc": "20123456789",
        "legal_name": "Cevichería Doña Rosa S.A.C.",
        "address": "Av. Larco 123, Trujillo",
        "igv_rate": "18.00",
        "boleta_series": "B001",
        "factura_series": "F001",
        "sol_user": "MODDATOS",
        "sol_password": "moddatos",
        **extra,
    }
    headers = authorization_for(restaurant.admin)
    response = await client.put(url("billing/settings"), json=body, headers=headers)
    assert response.status_code == 200, response.text
    if certificate:
        pfx, _ = make_certificate()
        response = await client.put(
            url("billing/settings/certificate"),
            json=certificate_upload(pfx),
            headers=headers,
        )
        assert response.status_code == 200, response.text


async def paid_order(
    client: AsyncClient,
    restaurant: StaffedRestaurant,
    menu: SeededMenu,
    discount: str | None = "10",
) -> dict[str, Any]:
    """Un pedido de dos lomos, servido y cobrado (con 10 % de descuento salvo que se pida otro)."""
    waiter, owner = authorization_for(restaurant.waiter), authorization_for(restaurant.admin)
    orders = url("orders")
    created = await client.post(
        orders,
        json={"type": "takeaway", "items": [{"menu_item_id": menu.lomo, "quantity": 2}]},
        headers=waiter,
    )
    order_id = created.json()["id"]
    for step, actor in (("send", waiter), ("ready", owner), ("served", waiter)):
        await client.post(f"{orders}/{order_id}/{step}", headers=actor)
    if discount is not None:
        await client.put(
            f"{orders}/{order_id}/discount",
            json={"percent": discount, "reason": "Cliente frecuente"},
            headers=waiter,
        )
    paid = await client.post(
        f"{orders}/{order_id}/charge", json={"payment_method": "yape"}, headers=waiter
    )
    assert paid.json()["status"] == "paid", paid.text
    return paid.json()
