"""Comprobantes: envío por correo (Brevo) y respaldo del PDF, el XML y el CDR (Google Drive).

Ninguna prueba llama a Brevo, a Google ni a Lycet: todo va por transportes simulados.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRow
from resthub.core.background import BackgroundJobs
from resthub.core.permissions import Permission
from resthub.modules.billing.adapters.api.dependencies import (
    get_document_archive,
    get_email_gateway,
    get_send_limit,
)
from resthub.modules.billing.adapters.archive.google_drive import GoogleDriveArchive
from resthub.modules.billing.adapters.email.brevo import BrevoEmailSender
from resthub.modules.billing.domain.exceptions import DocumentArchiveError, EmailDeliveryError
from resthub.modules.billing.domain.invoices import ArchiveStatus, mask_email
from resthub.modules.billing.domain.send_limit import SendLimit
from resthub.modules.billing.ports.delivery_ports import (
    DocumentFile,
    EmailAttachment,
    OutgoingEmail,
)
from tests.builders import SeededMenu, open_cash_session, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, staff_with_role, url
from tests.fakes import FIXED_NOW
from tests.fakes_billing_delivery import FakeArchive, FakeEmailGateway
from tests.fakes_lycet import (
    PDF as LYCET_PDF,
)
from tests.fakes_lycet import (
    FakeLycet,
    configure_billing,
    lycet_reply,
    paid_order,
    use_fake_lycet,
)

BILLING_URL = url("billing")


# -- Dominio y adaptadores ----------------------------------------------------


def test_mask_email_hides_the_local_part() -> None:
    assert mask_email("ana@correo.pe") == "a•••@correo.pe"
    assert mask_email("sin-arroba") == "•••"


def test_send_limit_counts_per_account_in_a_sliding_hour() -> None:
    limit = SendLimit(2)

    assert limit.try_take(1, FIXED_NOW)
    assert limit.try_take(1, FIXED_NOW)
    assert not limit.try_take(1, FIXED_NOW)
    # Otra cuenta tiene su propio tope; y una hora después la primera vuelve a poder.
    assert limit.try_take(2, FIXED_NOW)
    assert limit.try_take(1, FIXED_NOW + timedelta(hours=1))


EMAIL = OutgoingEmail(
    to_address="ana@correo.pe", to_name="Ana", subject="Asunto", html="<p>Hola</p>", text="Hola"
)


async def test_brevo_posts_the_message_with_the_api_key_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"messageId": "<1@brevo>"})

    sender = BrevoEmailSender(
        "clave-de-prueba", "caja@local.pe", "Local", httpx.MockTransport(handler)
    )

    await sender.send(EMAIL)

    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://api.brevo.com/v3/smtp/email"
    assert request.headers["api-key"] == "clave-de-prueba"
    assert body["sender"] == {"name": "Local", "email": "caja@local.pe"}
    assert body["to"] == [{"email": "ana@correo.pe", "name": "Ana"}]
    assert (body["subject"], body["htmlContent"], body["textContent"]) == (
        "Asunto",
        "<p>Hola</p>",
        "Hola",
    )
    # Sin adjuntos no se manda la clave: Brevo rechaza una lista vacía.
    assert "attachment" not in body


async def test_brevo_sends_the_attachments_in_base64() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"messageId": "<1@brevo>"})

    sender = BrevoEmailSender("clave", "caja@local.pe", "Local", httpx.MockTransport(handler))
    email = OutgoingEmail(
        to_address="ana@correo.pe",
        to_name="",
        subject="Asunto",
        html="<p>Hola</p>",
        text="Hola",
        attachments=(EmailAttachment("B001-1.pdf", b"%PDF-1.4"),),
    )

    await sender.send(email)

    body = json.loads(seen[0].content)
    assert body["attachment"] == [
        {"name": "B001-1.pdf", "content": base64.b64encode(b"%PDF-1.4").decode()}
    ]
    assert body["to"] == [{"email": "ana@correo.pe"}]


@pytest.mark.parametrize("status_code", [400, 401, 500])
async def test_brevo_error_becomes_a_delivery_error_without_the_address(status_code: int) -> None:
    sender = BrevoEmailSender(
        "clave",
        "caja@local.pe",
        "Local",
        httpx.MockTransport(
            lambda _: httpx.Response(
                status_code, json={"code": "invalid_parameter", "message": "ana@correo.pe mal"}
            )
        ),
    )

    with pytest.raises(EmailDeliveryError) as raised:
        await sender.send(EMAIL)

    assert "ana@correo.pe" not in str(raised.value)


async def test_brevo_unreachable_is_a_delivery_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        message = "sin red"
        raise httpx.ConnectError(message)

    sender = BrevoEmailSender("clave", "caja@local.pe", "Local", httpx.MockTransport(handler))

    with pytest.raises(EmailDeliveryError):
        await sender.send(EMAIL)


def test_brevo_is_off_without_key_or_sender() -> None:
    assert not BrevoEmailSender("", "caja@local.pe", "Local").is_enabled()
    assert not BrevoEmailSender("clave", "", "Local").is_enabled()
    assert BrevoEmailSender("clave", "caja@local.pe", "").is_enabled()


class FakeGoogle:
    """Google Drive y su servidor OAuth, en memoria."""

    def __init__(self, *, token_status: int = 200, expire_first_token: bool = False) -> None:
        self.token_status = token_status
        self.expire_first_token = expire_first_token
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.folders: dict[tuple[str, str], str] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.uploads: list[bytes] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(
                200, json={"access_token": f"token-{self.token_calls}", "expires_in": 3599}
            )
        if request.headers.get("authorization") == "Bearer token-1" and self.expire_first_token:
            return httpx.Response(401, json={"error": {"code": 401}})
        if request.method == "GET":
            return self._list(request)
        if path.startswith("/upload/"):
            return self._upload(request)
        body = json.loads(request.content)
        folder_id = f"folder-{len(self.folders) + 1}"
        self.folders[(body["parents"][0], body["name"])] = folder_id
        return httpx.Response(200, json={"id": folder_id})

    def _list(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params["q"]
        name = query.split("'")[1]
        parent = query.split("'")[3]
        if "!= 'application/vnd.google-apps.folder'" in query:
            link = self.files.get((parent, name))
            found = [{"id": "file-1", "webViewLink": link}] if link else []
        else:
            folder = self.folders.get((parent, name))
            found = [{"id": folder}] if folder else []
        return httpx.Response(200, json={"files": found})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"].startswith("multipart/related; boundary=")
        assert request.url.params["uploadType"] == "multipart"
        parts = request.content.split(b"\r\n")
        metadata = json.loads(parts[3])
        link = f"https://drive.google.com/file/{metadata['name']}"
        self.files[(metadata["parents"][0], metadata["name"])] = link
        self.uploads.append(request.content)
        return httpx.Response(200, json={"id": "file-1", "webViewLink": link})


def _drive(google: FakeGoogle) -> GoogleDriveArchive:
    return GoogleDriveArchive(
        "cliente", "secreto", "refresco", "raiz", httpx.MockTransport(google.handler)
    )


PDF = DocumentFile(content=b"%PDF-1.4 ...", media_type="application/pdf")
FOLDER = ("Cevichería Doña Rosa [1]", "2026", "2026-06")


async def test_drive_creates_the_folder_tree_and_uploads_the_file() -> None:
    google = FakeGoogle()

    link = await _drive(google).store(FOLDER, "B001-15.pdf", PDF)

    assert link == "https://drive.google.com/file/B001-15.pdf"
    assert [name for _, name in google.folders] == list(FOLDER)
    assert google.folders[("raiz", FOLDER[0])] == "folder-1"
    assert b"%PDF-1.4 ..." in google.uploads[0]
    refresh = google.requests[0]
    assert refresh.url.host == "oauth2.googleapis.com"
    assert b"grant_type=refresh_token" in refresh.content
    assert b"refresco" in refresh.content


async def test_drive_upload_is_idempotent_by_name_and_reuses_folders() -> None:
    google = FakeGoogle()
    drive = _drive(google)

    first = await drive.store(FOLDER, "B001-15.pdf", PDF)
    second = await drive.store(FOLDER, "B001-15.pdf", PDF)
    await drive.store(FOLDER, "B001-15.xml", PDF)

    assert first == second
    assert len(google.uploads) == 2
    assert len(google.folders) == 3
    assert google.token_calls == 1


async def test_drive_renews_an_expired_token_once() -> None:
    google = FakeGoogle(expire_first_token=True)

    await _drive(google).store(FOLDER, "B001-15.pdf", PDF)

    assert google.token_calls == 2


async def test_drive_rejected_credentials_ask_to_authorize_again() -> None:
    drive = _drive(FakeGoogle(token_status=400))

    with pytest.raises(DocumentArchiveError, match="drive_authorize"):
        await drive.store(FOLDER, "B001-15.pdf", PDF)


async def test_drive_escapes_quotes_in_folder_names() -> None:
    google = FakeGoogle()

    await _drive(google).store(("Pollería d'Ana", "2026", "2026-06"), "B001-1.pdf", PDF)

    queries = [r.url.params["q"] for r in google.requests if r.method == "GET"]
    assert "name = 'Pollería d\\'Ana'" in queries[0]


def test_drive_is_off_without_any_credential() -> None:
    assert not GoogleDriveArchive("", "s", "r", "f").is_enabled()
    assert not GoogleDriveArchive("c", "s", "r", "").is_enabled()
    assert GoogleDriveArchive("c", "s", "r", "f").is_enabled()


# -- HTTP ---------------------------------------------------------------------


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    menu = await seed_menu(session, restaurant_a.id)
    await open_cash_session(session, restaurant_a.id, restaurant_a.admin.id or 0)
    return menu


@pytest.fixture
def mail() -> FakeEmailGateway:
    return FakeEmailGateway()


@pytest.fixture
def drive() -> FakeArchive:
    return FakeArchive()


@pytest.fixture
def lycet(app: FastAPI) -> FakeLycet:
    """Lycet falso: también lo usa el respaldo en segundo plano (`get_invoicer`)."""
    fake = FakeLycet()
    use_fake_lycet(app, fake)
    return fake


@pytest.fixture
def wired(app: FastAPI, mail: FakeEmailGateway, drive: FakeArchive, lycet: FakeLycet) -> FastAPI:
    """La app con el correo y el respaldo encendidos, con dobles en memoria."""
    app.dependency_overrides[get_email_gateway] = lambda: mail
    app.dependency_overrides[get_document_archive] = lambda: drive
    return app


async def _issue(
    client: AsyncClient,
    restaurant: StaffedRestaurant,
    menu: SeededMenu,
    reply: dict[str, Any] | None = None,
    *,
    certificate: bool = True,
    fake: FakeLycet | None = None,
) -> dict[str, Any]:
    """Emitir una boleta; sin certificado queda «sin enviar» (sin documentos)."""
    app = client._transport.app  # type: ignore[attr-defined]
    use_fake_lycet(app, fake or FakeLycet(reply))
    await configure_billing(client, restaurant, certificate=certificate)
    order = await paid_order(client, restaurant, menu)
    created = await client.post(
        f"{BILLING_URL}/invoices",
        json={"order_id": order["id"], "kind": "boleta"},
        headers=authorization_for(restaurant.waiter),
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_capabilities_say_what_the_server_has_on(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(
        f"{BILLING_URL}/capabilities", headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 200
    assert response.json() == {
        "email_enabled": False,
        "email_max_per_hour": 20,
        "archive_enabled": False,
        "tax_invoice_enabled": True,
    }


async def test_capabilities_with_everything_on(
    wired: FastAPI, client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(
        f"{BILLING_URL}/capabilities", headers=authorization_for(restaurant_a.waiter)
    )

    assert response.json() == {
        "email_enabled": True,
        "email_max_per_hour": 20,
        "archive_enabled": True,
        "tax_invoice_enabled": True,
    }


async def test_capabilities_need_a_session(client: AsyncClient) -> None:
    assert (await client.get(f"{BILLING_URL}/capabilities")).status_code == 401


async def test_email_attaches_the_pdf_and_logs_the_masked_address(
    wired: FastAPI,
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    mail: FakeEmailGateway,
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)

    response = await client.post(
        f"{BILLING_URL}/invoices/{invoice['id']}/email",
        json={"to_email": "ana@correo.pe", "to_name": "Ana"},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"sent": True, "masked_to": "a•••@correo.pe"}
    [email] = mail.sent
    assert email.to_address == "ana@correo.pe"
    # El PDF va adjunto, armado por Lycet; ya no hay enlace.
    assert email.attachments == (EmailAttachment(f"{invoice['code']}.pdf", LYCET_PDF),)
    assert "adjunta en PDF" in email.text
    assert "href" not in email.html
    assert invoice["code"] in email.subject
    details = (
        await session.scalars(
            select(ActivityRow.detail).where(ActivityRow.kind == ActivityKind.INVOICE_EMAILED.value)
        )
    ).all()
    assert len(details) == 1
    assert "a•••@correo.pe" in details[0]
    assert "ana@correo.pe" not in details[0]


async def test_email_is_off_when_the_server_has_no_provider(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)

    response = await client.post(
        f"{BILLING_URL}/invoices/{invoice['id']}/email",
        json={"to_email": "ana@correo.pe"},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "email_sending_disabled"


async def test_email_provider_failure_is_a_clear_error(
    wired: FastAPI,
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    mail: FakeEmailGateway,
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)
    mail.fail = True

    response = await client.post(
        f"{BILLING_URL}/invoices/{invoice['id']}/email",
        json={"to_email": "ana@correo.pe"},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "email_delivery"


async def test_email_needs_a_valid_address_and_a_pdf(
    wired: FastAPI,
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
) -> None:
    unsent = await _issue(client, restaurant_a, menu_a, certificate=False)
    headers = authorization_for(restaurant_a.waiter)

    invalid = await client.post(
        f"{BILLING_URL}/invoices/{unsent['id']}/email",
        json={"to_email": "no-es-correo"},
        headers=headers,
    )
    without_pdf = await client.post(
        f"{BILLING_URL}/invoices/{unsent['id']}/email",
        json={"to_email": "ana@correo.pe"},
        headers=headers,
    )

    assert invalid.status_code == 422
    assert unsent["status"] == "simulated"
    assert without_pdf.status_code == 422
    assert without_pdf.json()["code"] == "invalid_invoice"


async def test_email_reports_when_lycet_cannot_build_the_pdf(
    wired: FastAPI,
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    mail: FakeEmailGateway,
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)
    broken = FakeLycet(pdf_status=502)
    use_fake_lycet(wired, broken)

    response = await client.post(
        f"{BILLING_URL}/invoices/{invoice['id']}/email",
        json={"to_email": "ana@correo.pe"},
        headers=authorization_for(restaurant_a.waiter),
    )

    assert response.status_code == 502
    assert response.json()["code"] == "billing_provider"
    assert mail.sent == []


async def test_email_is_limited_per_account(
    wired: FastAPI,
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    mail: FakeEmailGateway,
) -> None:
    limit = SendLimit(2)
    wired.dependency_overrides[get_send_limit] = lambda: limit
    invoice = await _issue(client, restaurant_a, menu_a)
    headers = authorization_for(restaurant_a.waiter)
    path = f"{BILLING_URL}/invoices/{invoice['id']}/email"

    statuses = [
        (await client.post(path, json={"to_email": "ana@correo.pe"}, headers=headers)).status_code
        for _ in range(3)
    ]
    other_account = await client.post(
        path, json={"to_email": "ana@correo.pe"}, headers=authorization_for(restaurant_a.admin)
    )

    assert statuses == [200, 200, 429]
    assert other_account.status_code == 200
    assert len(mail.sent) == 3


async def test_email_of_an_invoice_from_another_restaurant_is_a_404(
    wired: FastAPI,
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_a: SeededMenu,
    mail: FakeEmailGateway,
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)

    response = await client.post(
        f"{BILLING_URL}/invoices/{invoice['id']}/email",
        json={"to_email": "ana@correo.pe"},
        headers=authorization_for(restaurant_b.admin),
    )

    assert response.status_code == 404
    assert mail.sent == []


async def test_a_waiter_cannot_email_an_invoice_of_an_order_they_did_not_take(
    wired: FastAPI,
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    mail: FakeEmailGateway,
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)
    other = await staff_with_role(
        session, restaurant_a, "Otro mesero", {Permission.BILLING_ISSUE.value}
    )

    response = await client.post(
        f"{BILLING_URL}/invoices/{invoice['id']}/email",
        json={"to_email": "ana@correo.pe"},
        headers=authorization_for(other),
    )

    assert response.status_code == 404
    assert mail.sent == []


async def test_accepted_invoice_is_archived_in_the_background(
    wired: FastAPI,
    client: AsyncClient,
    jobs: BackgroundJobs,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    drive: FakeArchive,
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)
    assert invoice["archive_status"] == "pending"
    assert (invoice["has_xml"], invoice["has_cdr"], invoice["has_pdf"]) == (True, True, True)

    await jobs.drain()
    done = await client.get(
        f"{BILLING_URL}/invoices/{invoice['id']}", headers=authorization_for(restaurant_a.waiter)
    )

    assert done.json()["archive_status"] == "done"
    assert done.json()["archive_status_label"] == "Respaldado"
    assert done.json()["archived_at"] is not None
    code = invoice["code"]
    # El XML y el CDR guardados en el comprobante, y el PDF que arma Lycet.
    assert [name for _, name in drive.stored] == [f"{code}.xml", f"R-{code}.zip", f"{code}.pdf"]
    restaurant, year, month = drive.stored[0][0]
    assert restaurant.endswith(f"[{restaurant_a.id}]")
    assert len(year) == 4
    assert month.startswith(year)


async def test_archive_off_leaves_the_invoice_without_backup(
    client: AsyncClient, jobs: BackgroundJobs, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)

    await jobs.drain()

    assert invoice["archive_status"] == "disabled"
    assert invoice["has_xml"] is True


async def test_failed_backup_is_recorded_and_can_be_retried(
    wired: FastAPI,
    client: AsyncClient,
    jobs: BackgroundJobs,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    drive: FakeArchive,
) -> None:
    drive.fail = True
    invoice = await _issue(client, restaurant_a, menu_a)
    await jobs.drain()
    owner = authorization_for(restaurant_a.admin)

    failed = await client.get(f"{BILLING_URL}/invoices/{invoice['id']}", headers=owner)

    assert failed.json()["archive_status"] == "failed"
    assert "Google Drive" in failed.json()["archive_message"]
    assert failed.json()["archived_at"] is None

    drive.fail = False
    retry = await client.post(f"{BILLING_URL}/invoices/{invoice['id']}/archive", headers=owner)
    await jobs.drain()
    done = await client.get(f"{BILLING_URL}/invoices/{invoice['id']}", headers=owner)

    assert retry.status_code == 202
    assert retry.json()["archive_status"] == "pending"
    assert done.json()["archive_status"] == "done"
    again = await client.post(f"{BILLING_URL}/invoices/{invoice['id']}/archive", headers=owner)
    assert again.status_code == 422


async def test_pdf_failure_marks_the_backup_as_failed(
    wired: FastAPI,
    client: AsyncClient,
    jobs: BackgroundJobs,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    drive: FakeArchive,
) -> None:
    # Lycet acepta el comprobante, pero no puede armar el PDF cuando corre el respaldo.
    invoice = await _issue(client, restaurant_a, menu_a, fake=FakeLycet(pdf_status=502))

    await jobs.drain()
    read = await client.get(
        f"{BILLING_URL}/invoices/{invoice['id']}", headers=authorization_for(restaurant_a.admin)
    )

    assert read.json()["archive_status"] == "failed"
    assert read.json()["archive_message"] == (
        "El servicio de facturación no pudo armar el PDF. Inténtalo de nuevo."
    )
    # El XML y el CDR alcanzaron a subir; el reintento sube lo que falta sin duplicar.
    code = invoice["code"]
    assert [name for _, name in drive.stored] == [f"{code}.xml", f"R-{code}.zip"]


async def test_retry_needs_the_archive_on_a_manager_and_an_accepted_invoice(
    wired: FastAPI,
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
) -> None:
    rejected = await _issue(client, restaurant_a, menu_a, lycet_reply("rejected_2800"))
    path = f"{BILLING_URL}/invoices/{rejected['id']}/archive"

    waiter = await client.post(path, headers=authorization_for(restaurant_a.waiter))
    not_accepted = await client.post(path, headers=authorization_for(restaurant_a.admin))

    assert waiter.status_code == 403
    assert not_accepted.status_code == 422


async def test_retry_with_the_archive_off_is_a_conflict(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)

    response = await client.post(
        f"{BILLING_URL}/invoices/{invoice['id']}/archive",
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "archive_disabled"


async def test_archive_of_another_restaurants_invoice_is_a_404(
    wired: FastAPI,
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_a: SeededMenu,
) -> None:
    invoice = await _issue(client, restaurant_a, menu_a)

    response = await client.post(
        f"{BILLING_URL}/invoices/{invoice['id']}/archive",
        headers=authorization_for(restaurant_b.admin),
    )

    assert response.status_code == 404


def test_archive_status_labels_are_in_spanish() -> None:
    assert {status.value: status.label for status in ArchiveStatus} == {
        "disabled": "Sin respaldo",
        "pending": "Respaldo en curso",
        "done": "Respaldado",
        "failed": "El respaldo falló",
    }
