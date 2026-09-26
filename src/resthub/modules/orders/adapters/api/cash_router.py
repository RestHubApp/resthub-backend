"""Adaptador de entrada HTTP de la caja.

- `orders.charge` (mesero y encargado): saber si la caja está abierta.
- `cash.manage` (encargado): abrirla, cerrarla con el arqueo y ver los turnos.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.core.realtime_broker import EventPublisherDep
from resthub.modules.orders.adapters.api.dependencies import (
    CashRegisterDep,
    OrderRepositoryDep,
    StaffDirectoryDep,
)
from resthub.modules.orders.adapters.api.errors import http_error
from resthub.modules.orders.adapters.api.schemas import (
    CashSessionPageResponse,
    CashSessionResponse,
    CloseCashRequest,
    CurrentCashResponse,
    OpenCashRequest,
)
from resthub.modules.orders.domain.exceptions import OrdersError
from resthub.modules.orders.use_cases.cash import (
    CloseCash,
    CloseCashCommand,
    DescribeCash,
    ListCashSessions,
    OpenCash,
    OpenCashCommand,
    ReadCashSession,
    ReadCurrentCash,
)

router = APIRouter()

CashierDep = Annotated[Principal, Depends(require_permission(Permission.ORDERS_CHARGE))]
CashManagerDep = Annotated[Principal, Depends(require_permission(Permission.CASH_MANAGE))]


@router.get("/current", response_model=CurrentCashResponse, summary="La caja abierta, si hay")
async def read_current_cash(
    principal: CashierDep,
    cash: CashRegisterDep,
    orders: OrderRepositoryDep,
    staff: StaffDirectoryDep,
) -> CurrentCashResponse:
    session = await ReadCurrentCash(cash)(principal.restaurant_id)
    if session is None:
        return CurrentCashResponse(is_open=False, session=None)
    # El mesero solo necesita saber que puede cobrar; los montos del turno son
    # del encargado.
    if Permission.CASH_MANAGE not in principal.permissions:
        return CurrentCashResponse(is_open=True, session=None)
    view = await DescribeCash(cash, orders, staff)(session)
    return CurrentCashResponse(is_open=True, session=CashSessionResponse.from_view(view))


@router.post(
    "/open",
    response_model=CashSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Abrir la caja con el efectivo inicial",
)
async def open_cash(
    payload: OpenCashRequest,
    principal: CashManagerDep,
    cash: CashRegisterDep,
    orders: OrderRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> CashSessionResponse:
    try:
        session = await OpenCash(cash, activity, events)(
            OpenCashCommand(
                actor=principal, opening_amount=payload.opening_amount, notes=payload.notes
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    view = await DescribeCash(cash, orders, staff)(session)
    return CashSessionResponse.from_view(view)


@router.post("/close", response_model=CashSessionResponse, summary="Cerrar la caja con el arqueo")
async def close_cash(
    payload: CloseCashRequest,
    principal: CashManagerDep,
    cash: CashRegisterDep,
    orders: OrderRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> CashSessionResponse:
    describe = DescribeCash(cash, orders, staff)
    try:
        view = await CloseCash(cash, describe, activity, events)(
            CloseCashCommand(
                actor=principal, counted_cash=payload.counted_cash, notes=payload.notes
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return CashSessionResponse.from_view(view)


@router.get("/sessions", response_model=CashSessionPageResponse, summary="Turnos de caja")
async def list_cash_sessions(
    principal: CashManagerDep,
    cash: CashRegisterDep,
    staff: StaffDirectoryDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CashSessionPageResponse:
    page = await ListCashSessions(cash)(principal.restaurant_id, limit, offset)
    people = {session.opened_by for session in page.items} | {
        session.closed_by for session in page.items if session.closed_by is not None
    }
    names = await staff.names(principal.restaurant_id, people)
    return CashSessionPageResponse(
        items=[
            CashSessionResponse.from_session(
                session,
                names.get(session.opened_by, ""),
                names.get(session.closed_by, "") if session.closed_by else "",
            )
            for session in page.items
        ],
        total=page.total,
    )


@router.get(
    "/sessions/{session_id}", response_model=CashSessionResponse, summary="Arqueo de un turno"
)
async def read_cash_session(
    session_id: int,
    principal: CashManagerDep,
    cash: CashRegisterDep,
    orders: OrderRepositoryDep,
    staff: StaffDirectoryDep,
) -> CashSessionResponse:
    try:
        session = await ReadCashSession(cash)(principal.restaurant_id, session_id)
    except OrdersError as error:
        raise http_error(error) from error
    view = await DescribeCash(cash, orders, staff)(session)
    return CashSessionResponse.from_view(view)
