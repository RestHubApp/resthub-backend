"""Adaptador de entrada HTTP de los comprobantes electrónicos.

- `billing.issue` (mesero y encargado): emitir el comprobante de un pedido
  pagado y verlo.
- `billing.manage` (encargado): datos fiscales, listado y reenvío.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import SessionDep, require_permission
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.modules.billing.adapters.api.schemas import (
    BillingSettingsRequest,
    BillingSettingsResponse,
    InvoicePageResponse,
    InvoiceResponse,
    IssueInvoiceRequest,
)
from resthub.modules.billing.adapters.persistence.repositories import (
    SqlAlchemyBillingSettings,
    SqlAlchemyInvoiceRepository,
    SqlPaidOrderDirectory,
)
from resthub.modules.billing.adapters.sunat.nubefact import NubefactInvoicer
from resthub.modules.billing.domain.exceptions import (
    BillingError,
    InvoiceNotFound,
    InvoiceNumberTaken,
    OrderAlreadyInvoiced,
    PaidOrderNotFound,
)
from resthub.modules.billing.domain.invoices import Customer, InvoiceStatus
from resthub.modules.billing.ports.billing_ports import ElectronicInvoicer, InvoiceQuery
from resthub.modules.billing.use_cases.invoicing import (
    IssueInvoice,
    IssueInvoiceCommand,
    ListInvoices,
    ResendInvoice,
    SettingsChange,
    UpdateBillingSettings,
    find_invoice,
)

router = APIRouter()

IssuerDep = Annotated[Principal, Depends(require_permission(Permission.BILLING_ISSUE))]
ManagerDep = Annotated[Principal, Depends(require_permission(Permission.BILLING_MANAGE))]


def get_invoicer() -> ElectronicInvoicer:
    """El proveedor de comprobantes; las pruebas lo reemplazan por uno falso."""
    return NubefactInvoicer()


InvoicerDep = Annotated[ElectronicInvoicer, Depends(get_invoicer)]


def _http_error(error: BillingError) -> HTTPException:
    if isinstance(error, InvoiceNotFound | PaidOrderNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, OrderAlreadyInvoiced | InvoiceNumberTaken):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


@router.get("/settings", response_model=BillingSettingsResponse, summary="Datos fiscales")
async def read_settings(principal: ManagerDep, session: SessionDep) -> BillingSettingsResponse:
    settings = await SqlAlchemyBillingSettings(session).get(principal.restaurant_id)
    return BillingSettingsResponse.from_entity(settings)


@router.put("/settings", response_model=BillingSettingsResponse, summary="Editar datos fiscales")
async def update_settings(
    payload: BillingSettingsRequest,
    principal: ManagerDep,
    session: SessionDep,
    activity: ActivityRecorderDep,
) -> BillingSettingsResponse:
    try:
        settings = await UpdateBillingSettings(SqlAlchemyBillingSettings(session), activity)(
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
    session: SessionDep,
    invoicer: InvoicerDep,
    activity: ActivityRecorderDep,
) -> InvoiceResponse:
    issue = IssueInvoice(
        SqlAlchemyInvoiceRepository(session),
        SqlAlchemyBillingSettings(session),
        SqlPaidOrderDirectory(session),
        invoicer,
        activity,
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
    session: SessionDep,
    date_from: date | None = None,
    date_to: date | None = None,
    invoice_status: Annotated[InvoiceStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InvoicePageResponse:
    page = await ListInvoices(SqlAlchemyInvoiceRepository(session))(
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
    invoice_id: int, principal: IssuerDep, session: SessionDep
) -> InvoiceResponse:
    try:
        invoice = await find_invoice(
            SqlAlchemyInvoiceRepository(session), principal.restaurant_id, invoice_id
        )
    except BillingError as error:
        raise _http_error(error) from error
    return InvoiceResponse.from_entity(invoice)


@router.get(
    "/orders/{order_id}/invoice",
    response_model=InvoiceResponse,
    summary="El comprobante de un pedido, si ya se emitió",
)
async def invoice_for_order(
    order_id: int, principal: IssuerDep, session: SessionDep
) -> InvoiceResponse:
    invoice = await SqlAlchemyInvoiceRepository(session).for_order(
        principal.restaurant_id, order_id
    )
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El pedido todavía no tiene comprobante.")
    return InvoiceResponse.from_entity(invoice)


@router.post(
    "/invoices/{invoice_id}/resend",
    response_model=InvoiceResponse,
    summary="Reenviar un comprobante pendiente, rechazado o sin enviar",
)
async def resend_invoice(
    invoice_id: int, principal: ManagerDep, session: SessionDep, invoicer: InvoicerDep
) -> InvoiceResponse:
    resend = ResendInvoice(
        SqlAlchemyInvoiceRepository(session), SqlAlchemyBillingSettings(session), invoicer
    )
    try:
        invoice = await resend(principal.restaurant_id, invoice_id)
    except BillingError as error:
        raise _http_error(error) from error
    return InvoiceResponse.from_entity(invoice)
