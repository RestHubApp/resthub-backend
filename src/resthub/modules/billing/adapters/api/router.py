"""Adaptador de entrada HTTP de los comprobantes electrónicos.

- `billing.issue` (mesero y encargado): emitir el comprobante de un pedido
  pagado y verlo. Sin `orders.read_all`, solo de los pedidos que tomó uno
  mismo, como en `orders`; los de otro responden 404.
- `billing.manage` (encargado): datos fiscales, listado y reenvío.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.api_params import PathId
from resthub.core.auth import require_permission
from resthub.core.clock_dependency import ClockDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.modules.billing.adapters.api.dependencies import (
    BillingSettingsDep,
    InvoicerDep,
    InvoiceRepositoryDep,
    PaidOrderDirectoryDep,
)
from resthub.modules.billing.adapters.api.schemas import (
    BillingSettingsRequest,
    BillingSettingsResponse,
    InvoicePageResponse,
    InvoiceResponse,
    IssueInvoiceRequest,
)
from resthub.modules.billing.domain.exceptions import (
    BillingError,
    InvoiceNotFoundError,
    InvoiceNumberTakenError,
    OrderAlreadyInvoicedError,
    PaidOrderNotFoundError,
)
from resthub.modules.billing.domain.invoices import Customer, InvoiceStatus
from resthub.modules.billing.ports.billing_ports import InvoiceQuery
from resthub.modules.billing.use_cases.invoicing import (
    IssueInvoice,
    IssueInvoiceCommand,
    ListInvoices,
    ReadInvoice,
    ResendInvoice,
    SettingsChange,
    UpdateBillingSettings,
)

router = APIRouter()

IssuerDep = Annotated[Principal, Depends(require_permission(Permission.BILLING_ISSUE))]
ManagerDep = Annotated[Principal, Depends(require_permission(Permission.BILLING_MANAGE))]


def _http_error(error: BillingError) -> HTTPException:
    if isinstance(error, InvoiceNotFoundError | PaidOrderNotFoundError):
        return api_error(status.HTTP_404_NOT_FOUND, error)
    if isinstance(error, OrderAlreadyInvoicedError | InvoiceNumberTakenError):
        return api_error(status.HTTP_409_CONFLICT, error)
    return api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)


@router.get("/settings", response_model=BillingSettingsResponse, summary="Datos fiscales")
async def read_settings(
    principal: ManagerDep, settings_repository: BillingSettingsDep
) -> BillingSettingsResponse:
    settings = await settings_repository.get(principal.restaurant_id)
    return BillingSettingsResponse.from_entity(settings)


@router.put("/settings", response_model=BillingSettingsResponse, summary="Editar datos fiscales")
async def update_settings(
    payload: BillingSettingsRequest,
    principal: ManagerDep,
    settings_repository: BillingSettingsDep,
    activity: ActivityRecorderDep,
) -> BillingSettingsResponse:
    try:
        settings = await UpdateBillingSettings(settings_repository, activity)(
            principal.restaurant_id,
            principal.user_id,
            SettingsChange(**payload.model_dump()),
        )
    except BillingError as error:
        raise _http_error(error) from error
    return BillingSettingsResponse.from_entity(settings)


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
) -> InvoiceResponse:
    issue = IssueInvoice(
        invoices,
        settings_repository,
        orders,
        invoicer,
        activity,
        clock=clock,
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
) -> InvoiceResponse:
    resend = ResendInvoice(invoices, settings_repository, invoicer)
    try:
        invoice = await resend(principal.restaurant_id, invoice_id)
    except BillingError as error:
        raise _http_error(error) from error
    return InvoiceResponse.from_entity(invoice)
