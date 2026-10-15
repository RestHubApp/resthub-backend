"""Cableado del adaptador HTTP de facturación."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from resthub.core.auth import PrincipalDep
from resthub.core.config import get_settings
from resthub.core.database import SessionDep
from resthub.modules.billing.adapters.archive.google_drive import GoogleDriveArchive
from resthub.modules.billing.adapters.email.brevo import BrevoEmailSender
from resthub.modules.billing.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyBillingSettings,
    SqlAlchemyInvoiceRepository,
    SqlAlchemyPaidOrderDirectory,
    SqlAlchemySandboxDirectory,
)
from resthub.modules.billing.adapters.sunat.certificates import CryptographyCertificateReader
from resthub.modules.billing.adapters.sunat.lycet import LycetGateway
from resthub.modules.billing.adapters.sunat.sandbox import SandboxInvoicer
from resthub.modules.billing.domain.send_limit import SendLimit
from resthub.modules.billing.ports.billing_ports import (
    BillingSettingsRepository,
    CertificateReader,
    ElectronicInvoiceGateway,
    InvoiceRepository,
    PaidOrderDirectory,
)
from resthub.modules.billing.ports.delivery_ports import (
    DocumentArchiveGateway,
    EmailGateway,
    InvoiceArchiveHook,
)


def get_billing_settings(session: SessionDep) -> BillingSettingsRepository:
    return SqlAlchemyBillingSettings(session)


def get_invoice_repository(session: SessionDep) -> InvoiceRepository:
    return SqlAlchemyInvoiceRepository(session)


def get_paid_order_directory(session: SessionDep) -> PaidOrderDirectory:
    return SqlAlchemyPaidOrderDirectory(session)


def get_invoicer() -> ElectronicInvoiceGateway:
    """Lycet, el servicio de facturación del servidor; las pruebas lo reemplazan por uno falso."""
    settings = get_settings()
    return LycetGateway(settings.lycet_url, settings.lycet_token.get_secret_value())


async def get_restaurant_invoicer(
    principal: PrincipalDep,
    session: SessionDep,
    provider: Annotated[ElectronicInvoiceGateway, Depends(get_invoicer)],
) -> ElectronicInvoiceGateway:
    """El servicio de facturación que le toca al local de quien pide.

    El local de muestra nunca llama al servicio real, aunque tenga RUC, usuario
    SOL y certificado cargados: el suyo simula el envío y no registra nada.
    """
    if await SqlAlchemySandboxDirectory(session).is_sandbox(principal.restaurant_id):
        return SandboxInvoicer()
    return provider


def get_email_gateway() -> EmailGateway:
    """El correo por Brevo; sin clave queda apagado. Las pruebas lo reemplazan."""
    settings = get_settings()
    return BrevoEmailSender(
        settings.brevo_api_key, settings.email_from_address, settings.email_from_name
    )


@lru_cache
def get_send_limit() -> SendLimit:
    """El tope de correos por cuenta; vive en memoria, así que es uno por proceso."""
    return SendLimit(get_settings().email_max_per_hour)


@lru_cache
def get_document_archive() -> DocumentArchiveGateway:
    """El respaldo en Google Drive; uno por proceso para reutilizar su token y sus carpetas."""
    settings = get_settings()
    return GoogleDriveArchive(
        settings.google_drive_client_id,
        settings.google_drive_client_secret,
        settings.google_drive_refresh_token,
        settings.google_drive_folder_id,
    )


def get_certificate_reader() -> CertificateReader:
    return CryptographyCertificateReader()


class NoArchive:
    """El aviso por omisión: no respalda nada. `main.py` instala el que lanza la tarea."""

    def is_enabled(self) -> bool:
        return False

    def schedule(self, restaurant_id: int, invoice_id: int) -> None:
        """Sin respaldo no hay nada que lanzar."""


def get_invoice_archive_hook() -> InvoiceArchiveHook:
    return NoArchive()


BillingSettingsDep = Annotated[BillingSettingsRepository, Depends(get_billing_settings)]
InvoiceRepositoryDep = Annotated[InvoiceRepository, Depends(get_invoice_repository)]
PaidOrderDirectoryDep = Annotated[PaidOrderDirectory, Depends(get_paid_order_directory)]
InvoicerDep = Annotated[ElectronicInvoiceGateway, Depends(get_restaurant_invoicer)]
EmailGatewayDep = Annotated[EmailGateway, Depends(get_email_gateway)]
CertificateReaderDep = Annotated[CertificateReader, Depends(get_certificate_reader)]
SendLimitDep = Annotated[SendLimit, Depends(get_send_limit)]
InvoiceArchiveHookDep = Annotated[InvoiceArchiveHook, Depends(get_invoice_archive_hook)]
