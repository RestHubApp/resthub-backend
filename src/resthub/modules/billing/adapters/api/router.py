"""Adaptador de entrada HTTP de los comprobantes electrónicos.

- `billing.issue` (mesero y encargado): emitir el comprobante de un pedido
  pagado y verlo. Sin `orders.read_all`, solo de los pedidos que tomó uno
  mismo, como en `orders`; los de otro responden 404.
- `billing.manage` (encargado): datos fiscales, credenciales SOL y certificado
  digital, listado y reenvío.

Los documentos (PDF, XML firmado y CDR) se descargan con la misma regla que ver
el comprobante; el PDF lo arma el servicio de facturación al momento.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.api_params import PathId
from resthub.core.auth import require_permission
from resthub.core.clock_dependency import ClockDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.file_response import inline_file_response
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.modules.billing.adapters.api.dependencies import (
    BillingSettingsDep,
    CertificateReaderDep,
    EmailGatewayDep,
    InvoiceArchiveHookDep,
    InvoicerDep,
    InvoiceRepositoryDep,
    PaidOrderDirectoryDep,
    SendLimitDep,
)
from resthub.modules.billing.adapters.api.schemas import (
    MAX_CERTIFICATE_BYTES,
    BillingCapabilitiesResponse,
    BillingSettingsRequest,
    BillingSettingsResponse,
    CertificateUploadRequest,
    InvoicePageResponse,
    InvoiceResponse,
    IssueInvoiceRequest,
    SendInvoiceEmailRequest,
    SendInvoiceEmailResponse,
)
from resthub.modules.billing.domain.exceptions import (
    ArchiveDisabledError,
    BillingError,
    BillingProviderError,
    EmailDeliveryError,
    EmailRateLimitedError,
    EmailSendingDisabledError,
    InvalidCertificateError,
    InvoiceDocumentUnavailableError,
    InvoiceNotFoundError,
    InvoiceNumberTakenError,
    OrderAlreadyInvoicedError,
    PaidOrderNotFoundError,
)
from resthub.modules.billing.domain.invoices import Customer, InvoiceStatus, mask_email
from resthub.modules.billing.ports.billing_ports import InvoiceQuery
from resthub.modules.billing.use_cases.archiving import RequestArchive
from resthub.modules.billing.use_cases.delivery import SendInvoiceEmail, SendInvoiceEmailCommand
from resthub.modules.billing.use_cases.invoicing import (
    DocumentFormat,
    IssueInvoice,
    IssueInvoiceCommand,
    ListInvoices,
    ReadInvoice,
    ReadInvoiceDocument,
    ResendInvoice,
    SettingsChange,
    UpdateBillingSettings,
    UploadCertificate,
    UploadCertificateCommand,
)

router = APIRouter()

IssuerDep = Annotated[Principal, Depends(require_permission(Permission.BILLING_ISSUE))]
ManagerDep = Annotated[Principal, Depends(require_permission(Permission.BILLING_MANAGE))]


def _http_error(error: BillingError) -> HTTPException:
    if isinstance(error, InvoiceNotFoundError | PaidOrderNotFoundError):
        return api_error(status.HTTP_404_NOT_FOUND, error)
    if isinstance(
        error,
        OrderAlreadyInvoicedError
        | InvoiceNumberTakenError
        | EmailSendingDisabledError
        | EmailDeliveryError
        | ArchiveDisabledError
        | InvoiceDocumentUnavailableError,
    ):
        return api_error(status.HTTP_409_CONFLICT, error)
    if isinstance(error, BillingProviderError):
        return api_error(status.HTTP_502_BAD_GATEWAY, error)
    if isinstance(error, EmailRateLimitedError):
        return api_error(status.HTTP_429_TOO_MANY_REQUESTS, error)
    return api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)


def _error(description: str) -> dict[str, object]:
    """Una respuesta de error documentada con el esquema común `ErrorResponse`."""
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }


def _unprocessable(description: str) -> dict[str, object]:
    """El 422 declarado a mano: la lista de validación de FastAPI o el error de una regla.

    Declararlo reemplaza el 422 que FastAPI documenta solo, así que lleva las dos formas.
    """
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                        {"$ref": "#/components/schemas/ErrorResponse"},
                    ]
                }
            }
        },
    }


_PROVIDER_DOWN = _error(
    "`billing_provider`: el servicio de facturación (Lycet) no responde o falla; reintentar."
)
_SETTINGS_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: _unprocessable(
        "`invalid_billing_settings` (datos fiscales o credenciales SOL inválidos) o "
        "`invalid_billing_credentials` (el servicio de facturación rechazó el usuario SOL, "
        "la clave o el certificado)."
    ),
    status.HTTP_502_BAD_GATEWAY: _PROVIDER_DOWN,
}
_CERTIFICATE_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: _unprocessable(
        "`invalid_certificate` (el archivo no abre, la contraseña no corresponde, no es RSA, "
        "pesa más de 64 KB o está vencido) o `invalid_billing_credentials` (el servicio de "
        "facturación lo rechazó)."
    ),
    status.HTTP_502_BAD_GATEWAY: _PROVIDER_DOWN,
}


@router.get("/settings", response_model=BillingSettingsResponse, summary="Datos fiscales")
async def read_settings(
    principal: ManagerDep, settings_repository: BillingSettingsDep, clock: ClockDep
) -> BillingSettingsResponse:
    settings = await settings_repository.get(principal.restaurant_id)
    return BillingSettingsResponse.from_entity(settings, clock.now())


@router.put(
    "/settings",
    response_model=BillingSettingsResponse,
    responses=_SETTINGS_RESPONSES,
    summary="Editar datos fiscales",
)
async def update_settings(
    payload: BillingSettingsRequest,
    principal: ManagerDep,
    settings_repository: BillingSettingsDep,
    activity: ActivityRecorderDep,
    invoicer: InvoicerDep,
    clock: ClockDep,
) -> BillingSettingsResponse:
    try:
        settings = await UpdateBillingSettings(settings_repository, activity, invoicer)(
            principal.restaurant_id,
            principal.user_id,
            SettingsChange(**payload.model_dump()),
        )
    except BillingError as error:
        raise _http_error(error) from error
    return BillingSettingsResponse.from_entity(settings, clock.now())


@router.put(
    "/settings/certificate",
    response_model=BillingSettingsResponse,
    responses=_CERTIFICATE_RESPONSES,
    summary="Subir o reemplazar el certificado digital (.pfx, .p12 o .pem)",
)
async def upload_certificate(
    payload: CertificateUploadRequest,
    principal: ManagerDep,
    settings_repository: BillingSettingsDep,
    reader: CertificateReaderDep,
    invoicer: InvoicerDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> BillingSettingsResponse:
    upload = UploadCertificate(settings_repository, reader, invoicer, activity, clock)
    try:
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except (binascii.Error, ValueError):
            message = "El certificado no llegó bien (base64 inválido)."
            raise InvalidCertificateError(message) from None
        if len(content) > MAX_CERTIFICATE_BYTES:
            message = f"El certificado pesa más de {MAX_CERTIFICATE_BYTES // 1024} KB."
            raise InvalidCertificateError(message)
        settings = await upload(
            UploadCertificateCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                content=content,
                password=payload.password,
            )
        )
    except BillingError as error:
        raise _http_error(error) from error
    return BillingSettingsResponse.from_entity(settings, clock.now())


@router.post(
    "/invoices",
    response_model=InvoiceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Emitir la boleta o factura de un pedido pagado",
)
async def issue_invoice(
    payload: IssueInvoiceRequest,
    principal: IssuerDep,
    invoices: InvoiceRepositoryDep,
    settings_repository: BillingSettingsDep,
    orders: PaidOrderDirectoryDep,
    invoicer: InvoicerDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
    archive: InvoiceArchiveHookDep,
) -> InvoiceResponse:
    issue = IssueInvoice(
        invoices,
        settings_repository,
        orders,
        invoicer,
        activity,
        clock=clock,
        archive=archive,
    )
    try:
        invoice = await issue(
            IssueInvoiceCommand(
                actor=principal,
                order_id=payload.order_id,
                kind=payload.kind,
                customer=Customer(
                    document_type=payload.customer_document_type,
                    document_number=payload.customer_document_number,
                    name=payload.customer_name,
                    address=payload.customer_address,
                ),
            )
        )
    except BillingError as error:
        raise _http_error(error) from error
    return InvoiceResponse.from_entity(invoice)


@router.get("/invoices", response_model=InvoicePageResponse, summary="Comprobantes emitidos")
async def list_invoices(
    principal: ManagerDep,
    invoices: InvoiceRepositoryDep,
    date_from: date | None = None,
    date_to: date | None = None,
    invoice_status: Annotated[InvoiceStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> InvoicePageResponse:
    page = await ListInvoices(invoices)(
        InvoiceQuery(
            restaurant_id=principal.restaurant_id,
            date_from=date_from,
            date_to=date_to,
            status=invoice_status,
            limit=limit,
            offset=offset,
        )
    )
    return InvoicePageResponse(
        items=[InvoiceResponse.from_entity(invoice) for invoice in page.items], total=page.total
    )


@router.get("/invoices/{invoice_id}", response_model=InvoiceResponse, summary="Un comprobante")
async def read_invoice(
    invoice_id: PathId,
    principal: IssuerDep,
    invoices: InvoiceRepositoryDep,
    orders: PaidOrderDirectoryDep,
) -> InvoiceResponse:
    read = ReadInvoice(invoices, orders)
    try:
        invoice = await read.by_id(principal, invoice_id)
    except BillingError as error:
        raise _http_error(error) from error
    return InvoiceResponse.from_entity(invoice)


@router.get(
    "/orders/{order_id}/invoice",
    response_model=InvoiceResponse,
    summary="El comprobante de un pedido, si ya se emitió",
)
async def invoice_for_order(
    order_id: PathId,
    principal: IssuerDep,
    invoices: InvoiceRepositoryDep,
    orders: PaidOrderDirectoryDep,
) -> InvoiceResponse:
    read = ReadInvoice(invoices, orders)
    invoice = await read.for_order(principal, order_id)
    if invoice is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "El pedido todavía no tiene comprobante.",
            ErrorCode.INVOICE_NOT_FOUND,
        )
    return InvoiceResponse.from_entity(invoice)


@router.post(
    "/invoices/{invoice_id}/resend",
    response_model=InvoiceResponse,
    summary="Reenviar un comprobante pendiente, rechazado o sin enviar",
)
async def resend_invoice(
    invoice_id: PathId,
    principal: ManagerDep,
    invoices: InvoiceRepositoryDep,
    settings_repository: BillingSettingsDep,
    invoicer: InvoicerDep,
    archive: InvoiceArchiveHookDep,
    clock: ClockDep,
) -> InvoiceResponse:
    resend = ResendInvoice(invoices, settings_repository, invoicer, archive, clock=clock)
    try:
        invoice = await resend(principal.restaurant_id, invoice_id)
    except BillingError as error:
        raise _http_error(error) from error
    return InvoiceResponse.from_entity(invoice)


@router.get(
    "/capabilities",
    response_model=BillingCapabilitiesResponse,
    summary="Qué funciones opcionales tiene encendidas el servidor",
)
async def read_capabilities(
    principal: IssuerDep,  # noqa: ARG001 - solo exige sesión con permiso de emitir
    gateway: EmailGatewayDep,
    send_limit: SendLimitDep,
    archive: InvoiceArchiveHookDep,
) -> BillingCapabilitiesResponse:
    return BillingCapabilitiesResponse(
        email_enabled=gateway.is_enabled(),
        email_max_per_hour=send_limit.max_per_window,
        archive_enabled=archive.is_enabled(),
    )


@router.post(
    "/invoices/{invoice_id}/email",
    response_model=SendInvoiceEmailResponse,
    summary="Enviar el comprobante por correo al cliente",
)
async def email_invoice(
    invoice_id: PathId,
    payload: SendInvoiceEmailRequest,
    principal: IssuerDep,
    invoices: InvoiceRepositoryDep,
    orders: PaidOrderDirectoryDep,
    settings_repository: BillingSettingsDep,
    gateway: EmailGatewayDep,
    invoicer: InvoicerDep,
    send_limit: SendLimitDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> SendInvoiceEmailResponse:
    send = SendInvoiceEmail(
        invoices=invoices,
        orders=orders,
        settings=settings_repository,
        gateway=gateway,
        invoicer=invoicer,
        activity=activity,
        limit=send_limit,
        clock=clock,
    )
    try:
        await send(
            SendInvoiceEmailCommand(
                actor=principal,
                invoice_id=invoice_id,
                to_address=str(payload.to_email),
                to_name=payload.to_name,
            )
        )
    except BillingError as error:
        raise _http_error(error) from error
    return SendInvoiceEmailResponse(sent=True, masked_to=mask_email(str(payload.to_email)))


@router.post(
    "/invoices/{invoice_id}/archive",
    response_model=InvoiceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Pedir o reintentar el respaldo del XML y el PDF en Google Drive",
)
async def archive_invoice(
    invoice_id: PathId,
    principal: ManagerDep,
    invoices: InvoiceRepositoryDep,
    archive: InvoiceArchiveHookDep,
) -> InvoiceResponse:
    try:
        invoice = await RequestArchive(invoices, archive)(principal.restaurant_id, invoice_id)
    except BillingError as error:
        raise _http_error(error) from error
    return InvoiceResponse.from_entity(invoice)


_DOCUMENT_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_200_OK: {
        "content": {
            "application/pdf": {},
            "application/xml": {},
            "application/zip": {},
        },
        "description": "El archivo del comprobante.",
    },
    status.HTTP_409_CONFLICT: _error(
        "`invoice_document_unavailable`: el comprobante todavía no tiene ese documento "
        "(el PDF solo existe para uno aceptado por SUNAT)."
    ),
    status.HTTP_502_BAD_GATEWAY: _PROVIDER_DOWN,
}


@router.get(
    "/invoices/{invoice_id}/{document_format}",
    response_class=Response,
    responses=_DOCUMENT_RESPONSES,
    summary="Descargar el PDF, el XML firmado o la constancia de SUNAT (CDR) de un comprobante",
)
async def download_invoice_document(
    invoice_id: PathId,
    document_format: DocumentFormat,
    principal: IssuerDep,
    invoices: InvoiceRepositoryDep,
    orders: PaidOrderDirectoryDep,
    invoicer: InvoicerDep,
) -> Response:
    read = ReadInvoiceDocument(invoices, orders, invoicer)
    try:
        document, filename = await read(principal, invoice_id, document_format)
    except BillingError as error:
        raise _http_error(error) from error
    return inline_file_response(document.content, document.media_type, filename)
