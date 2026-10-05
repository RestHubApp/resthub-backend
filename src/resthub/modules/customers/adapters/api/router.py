"""Adaptador de entrada HTTP de clientes.

- `customers.read` (mesero y encargado): buscar y ver la ficha.
- `customers.manage` (mesero y encargado): dar de alta y editar. El mesero
  registra al cliente nuevo al tomar su primer delivery.

El alta exige `consent: true`: el cliente aceptó el tratamiento de sus datos
(Ley N.º 29733). Sin eso responde 422 y no guarda nada.

- `customers.erase` (el encargado): derechos ARCO. Exportar todo lo que el
  local guarda de un cliente y borrar sus datos, que no se deshace.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import SessionDep, require_permission
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.modules.customers.adapters.api.dependencies import LinkedRecordsDep
from resthub.modules.customers.adapters.persistence.repositories import (
    SqlAlchemyCustomerRepository,
    SqlCustomerHistory,
)
from resthub.modules.customers.domain.customers import (
    MAX_ADDRESS_LENGTH,
    MAX_EMAIL_LENGTH,
    MAX_NAME_LENGTH,
    MAX_NOTES_LENGTH,
    MAX_PHONE_LENGTH,
    MAX_REFERENCE_LENGTH,
)
from resthub.modules.customers.domain.exceptions import (
    ConsentRequired,
    CustomerHasActiveOrders,
    CustomerNotFound,
    CustomersError,
    PhoneTaken,
)
from resthub.modules.customers.ports.customer_repository import CustomerQuery
from resthub.modules.customers.use_cases.customer_rights import (
    AnonymizeCustomer,
    CustomerExport,
    ExportCustomerData,
)
from resthub.modules.customers.use_cases.manage_customers import (
    CustomerCard,
    CustomerData,
    ReadCustomerCard,
    SaveCustomer,
    SearchCustomers,
)

router = APIRouter()

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.CUSTOMERS_READ))]
ManagerDep = Annotated[Principal, Depends(require_permission(Permission.CUSTOMERS_MANAGE))]
EraserDep = Annotated[Principal, Depends(require_permission(Permission.CUSTOMERS_ERASE))]


class CustomerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    phone: str = Field(default="", max_length=MAX_PHONE_LENGTH)
    email: str = Field(default="", max_length=MAX_EMAIL_LENGTH)
    address: str = Field(default="", max_length=MAX_ADDRESS_LENGTH)
    reference: str = Field(default="", max_length=MAX_REFERENCE_LENGTH)
    notes: str = Field(default="", max_length=MAX_NOTES_LENGTH)
    # Obligatorio en el alta. En una edición, `true` anota el consentimiento de
    # un cliente guardado antes de pedirlo; `false` no retira el ya dado.
    consent: bool = False


class CustomerOrderResponse(BaseModel):
    order_id: int
    number: int
    type: str
    status: str
    total: Decimal
    created_at: datetime


class CustomerResponse(BaseModel):
    id: int
    name: str
    phone: str
    email: str
    address: str
    reference: str
    notes: str
    visits: int
    spent: Decimal
    average_ticket: Decimal
    last_visit: datetime | None
    # Desde la tercera visita pagada.
    is_frequent: bool
    recent_orders: list[CustomerOrderResponse]
    created_at: datetime
    # Cuándo aceptó el tratamiento de sus datos y sobre qué versión del texto;
    # `null` si se guardó antes de pedirlo.
    consent_at: datetime | None
    consent_version: str | None

    @classmethod
    def from_card(cls, card: CustomerCard) -> CustomerResponse:
        customer, stats = card.customer, card.stats
        return cls(
            id=customer.id or 0,
            name=customer.name,
            phone=customer.phone,
            email=customer.email,
            address=customer.address,
            reference=customer.reference,
            notes=customer.notes,
            visits=stats.visits,
            spent=stats.spent,
            average_ticket=stats.average_ticket,
            last_visit=stats.last_visit,
            is_frequent=stats.is_frequent,
            recent_orders=[
                CustomerOrderResponse(
                    order_id=o.order_id,
                    number=o.number,
                    type=o.type,
                    status=o.status,
                    total=o.total,
                    created_at=o.created_at,
                )
                for o in card.recent_orders
            ],
            created_at=customer.created_at,
            consent_at=customer.consent.given_at if customer.consent else None,
            consent_version=customer.consent.version if customer.consent else None,
        )


class ExportedOrderResponse(BaseModel):
    number: int
    type: str
    status: str
    total: Decimal
    created_at: datetime
    customer_name: str
    customer_phone: str
    delivery_address: str
    delivery_reference: str
    notes: str


class ExportedReservationResponse(BaseModel):
    reserved_for: datetime
    party_size: int
    status: str
    customer_name: str
    phone: str
    notes: str


class CustomerExportResponse(BaseModel):
    """Derecho de acceso: todo lo que el local guarda de un cliente."""

    exported_at: datetime
    name: str
    phone: str
    email: str
    address: str
    reference: str
    notes: str
    created_at: datetime
    consent_at: datetime | None
    consent_version: str | None
    orders: list[ExportedOrderResponse]
    reservations: list[ExportedReservationResponse]

    @classmethod
    def from_export(cls, export: CustomerExport) -> CustomerExportResponse:
        customer, consent = export.customer, export.customer.consent
        return cls(
            exported_at=export.exported_at,
            name=customer.name,
            phone=customer.phone,
            email=customer.email,
            address=customer.address,
            reference=customer.reference,
            notes=customer.notes,
            created_at=customer.created_at,
            consent_at=consent.given_at if consent else None,
            consent_version=consent.version if consent else None,
            orders=[ExportedOrderResponse(**asdict(o)) for o in export.orders],
            reservations=[ExportedReservationResponse(**asdict(r)) for r in export.reservations],
        )


class CustomerPageResponse(BaseModel):
    items: list[CustomerResponse]
    total: int


def _http_error(error: CustomersError) -> HTTPException:
    if isinstance(error, CustomerNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, PhoneTaken):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    if isinstance(error, ConsentRequired):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))
    if isinstance(error, CustomerHasActiveOrders):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


@router.get("", response_model=CustomerPageResponse, summary="Buscar clientes")
async def search_customers(
    principal: ReaderDep,
    session: SessionDep,
    q: Annotated[str, Query(max_length=80, description="Nombre o teléfono")] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CustomerPageResponse:
    page = await SearchCustomers(
        SqlAlchemyCustomerRepository(session), SqlCustomerHistory(session)
    )(CustomerQuery(restaurant_id=principal.restaurant_id, text=q, limit=limit, offset=offset))
    return CustomerPageResponse(
        items=[CustomerResponse.from_card(card) for card in page.items], total=page.total
    )


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta un cliente",
)
async def create_customer(
    payload: CustomerRequest,
    principal: ManagerDep,
    session: SessionDep,
    activity: ActivityRecorderDep,
) -> CustomerResponse:
    try:
        customer = await SaveCustomer(SqlAlchemyCustomerRepository(session), activity)(
            principal.restaurant_id, principal.user_id, CustomerData(**payload.model_dump())
        )
    except CustomersError as error:
        raise _http_error(error) from error
    return await _card(principal, session, customer.id or 0)


@router.get("/{customer_id}", response_model=CustomerResponse, summary="Ficha del cliente")
async def read_customer(
    customer_id: int, principal: ReaderDep, session: SessionDep
) -> CustomerResponse:
    return await _card(principal, session, customer_id)


@router.put("/{customer_id}", response_model=CustomerResponse, summary="Editar un cliente")
async def update_customer(
    customer_id: int,
    payload: CustomerRequest,
    principal: ManagerDep,
    session: SessionDep,
    activity: ActivityRecorderDep,
) -> CustomerResponse:
    try:
        await SaveCustomer(SqlAlchemyCustomerRepository(session), activity)(
            principal.restaurant_id,
            principal.user_id,
            CustomerData(**payload.model_dump()),
            customer_id,
        )
    except CustomersError as error:
        raise _http_error(error) from error
    return await _card(principal, session, customer_id)


@router.get(
    "/{customer_id}/export",
    response_model=CustomerExportResponse,
    summary="Exportar todo lo que se guarda de un cliente (derecho de acceso)",
)
async def export_customer(
    customer_id: int,
    principal: EraserDep,
    session: SessionDep,
    linked: LinkedRecordsDep,
    activity: ActivityRecorderDep,
) -> CustomerExportResponse:
    try:
        export = await ExportCustomerData(SqlAlchemyCustomerRepository(session), linked, activity)(
            principal.restaurant_id, principal.user_id, customer_id
        )
    except CustomersError as error:
        raise _http_error(error) from error
    return CustomerExportResponse.from_export(export)


@router.post(
    "/{customer_id}/anonymize",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar los datos de un cliente (derecho de cancelación)",
)
async def anonymize_customer(
    customer_id: int,
    principal: EraserDep,
    session: SessionDep,
    linked: LinkedRecordsDep,
    activity: ActivityRecorderDep,
) -> Response:
    """No se deshace: borra sus datos de la ficha, sus pedidos y sus reservas.

    Los comprobantes se conservan, como pide la ley tributaria. 409 si tiene
    pedidos en curso.
    """
    try:
        await AnonymizeCustomer(SqlAlchemyCustomerRepository(session), linked, activity)(
            principal.restaurant_id, principal.user_id, customer_id
        )
    except CustomersError as error:
        raise _http_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _card(principal: Principal, session: SessionDep, customer_id: int) -> CustomerResponse:
    try:
        card = await ReadCustomerCard(
            SqlAlchemyCustomerRepository(session), SqlCustomerHistory(session)
        )(principal.restaurant_id, customer_id)
    except CustomersError as error:
        raise _http_error(error) from error
    return CustomerResponse.from_card(card)
