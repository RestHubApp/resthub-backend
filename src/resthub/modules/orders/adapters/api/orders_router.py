"""Adaptador de entrada HTTP de los pedidos.

Quién hace qué:
- `orders.take` (mesero y encargado): abrir, cargar platos, enviar a cocina,
  marcar servido.
- `orders.manage` (encargado): marcar listo, anular un plato ya enviado y cancelar.
- `orders.charge` (mesero y encargado): cobrar, entero o por partes, y
  descontar hasta el tope del mesero. El mesero, solo los pedidos que tomó.
- `orders.discount_any` (encargado): descontar sin tope e invitar platos.

Leer exige `orders.take` u `orders.read_all`; qué pedidos ve cada uno lo decide
el caso de uso. Un pedido que no le toca ver responde 404, igual que uno de
otro restaurante.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.api_params import MAX_ID, PathId
from resthub.core.auth import require_permission
from resthub.core.clock_dependency import ClockDep
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.core.realtime_broker import EventPublisherDep
from resthub.modules.orders.adapters.api.dependencies import (
    CashRegisterDep,
    ConsumedItemsReaderDep,
    CustomerDirectoryDep,
    DiscountPolicyDep,
    InvoiceStatusReaderDep,
    MenuCatalogDep,
    OrderRepositoryDep,
    RestaurantTimezoneReaderDep,
    SentToKitchenHookDep,
    ServedOrderHookDep,
    StaffDirectoryDep,
    TableRepositoryDep,
)
from resthub.modules.orders.adapters.api.errors import http_error
from resthub.modules.orders.adapters.api.schemas import (
    AddItemsRequest,
    CancelOrderRequest,
    ChangeItemRequest,
    ChargeOrderRequest,
    CourtesyRequest,
    DiscountRequest,
    MergeOrdersRequest,
    MoveOrderRequest,
    NewItemRequest,
    OpenOrderRequest,
    OrderPageResponse,
    OrderResponse,
    PaymentRequest,
    UpdateOrderRequest,
    VoidItemRequest,
    VoidPaymentRequest,
)
from resthub.modules.orders.domain.exceptions import OrdersError
from resthub.modules.orders.domain.orders import Order, OrderStatus, OrderType
from resthub.modules.orders.use_cases.adjustments import (
    ApplyDiscount,
    ApplyDiscountCommand,
    CourtesyCommand,
    SetCourtesy,
)
from resthub.modules.orders.use_cases.charge_order import ChargeOrder, ChargeOrderCommand
from resthub.modules.orders.use_cases.kitchen import (
    CancelOrder,
    CancelOrderCommand,
    MarkReady,
    VoidItem,
    VoidItemCommand,
)
from resthub.modules.orders.use_cases.move_orders import (
    MergeOrders,
    MergeOrdersCommand,
    MoveOrder,
    MoveOrderCommand,
)
from resthub.modules.orders.use_cases.read_orders import (
    ListActiveOrders,
    ListOrders,
    ListOrdersQuery,
    ReadOrder,
)
from resthub.modules.orders.use_cases.shared import DescribeOrders
from resthub.modules.orders.use_cases.take_orders import (
    AddItems,
    AddItemsCommand,
    ChangeItem,
    ChangeItemCommand,
    MarkServed,
    NewItem,
    OpenOrder,
    OpenOrderCommand,
    RemoveItem,
    RemoveItemCommand,
    SendToKitchen,
    UpdateOrderDetails,
    UpdateOrderDetailsCommand,
)
from resthub.modules.orders.use_cases.void_payment import VoidPayment, VoidPaymentCommand

router = APIRouter()

OrderReaderDep = Annotated[
    Principal,
    Depends(require_permission(Permission.ORDERS_TAKE, Permission.ORDERS_READ_ALL)),
]
OrderTakerDep = Annotated[Principal, Depends(require_permission(Permission.ORDERS_TAKE))]
KitchenManagerDep = Annotated[Principal, Depends(require_permission(Permission.ORDERS_MANAGE))]
CashierDep = Annotated[Principal, Depends(require_permission(Permission.ORDERS_CHARGE))]
CashManagerDep = Annotated[Principal, Depends(require_permission(Permission.CASH_MANAGE))]
DiscountManagerDep = Annotated[
    Principal, Depends(require_permission(Permission.ORDERS_DISCOUNT_ANY))
]


async def _respond(
    principal: Principal,
    order: Order,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
) -> OrderResponse:
    view = await DescribeOrders(tables, staff).one(principal.restaurant_id, order)
    return OrderResponse.from_view(view)


def _delivery(payload: UpdateOrderRequest) -> tuple[str | None, str | None, str | None] | None:
    """Teléfono, dirección y referencia; lo que no vino queda como estaba."""
    fields = (payload.customer_phone, payload.delivery_address, payload.delivery_reference)
    return None if all(field is None for field in fields) else fields


def _new_items(items: list[NewItemRequest]) -> tuple[NewItem, ...]:
    return tuple(
        NewItem(
            menu_item_id=item.menu_item_id,
            quantity=item.quantity,
            notes=item.notes,
            modifiers=tuple((m.group, m.option) for m in item.modifiers),
        )
        for item in items
    )


@router.get("", response_model=OrderPageResponse, summary="Historial de pedidos")
async def list_orders(
    principal: OrderReaderDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    order_status: Annotated[
        list[OrderStatus] | None, Query(alias="status", description="Filtra por estado")
    ] = None,
    date_from: Annotated[date | None, Query(description="Desde este día del local")] = None,
    date_to: Annotated[date | None, Query(description="Hasta este día del local")] = None,
    order_type: Annotated[OrderType | None, Query(alias="type")] = None,
    table_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    waiter_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> OrderPageResponse:
    page = await ListOrders(orders)(
        ListOrdersQuery(
            actor=principal,
            statuses=frozenset(order_status) if order_status else None,
            date_from=date_from,
            date_to=date_to,
            type=order_type,
            table_id=table_id,
            waiter_id=waiter_id,
            limit=limit,
            offset=offset,
        )
    )
    views = await DescribeOrders(tables, staff)(principal.restaurant_id, page.items)
    return OrderPageResponse(
        items=[OrderResponse.from_view(view) for view in views], total=page.total
    )


@router.get(
    "/active",
    response_model=list[OrderResponse],
    summary="Pedidos en curso, del más antiguo al más nuevo (tablero)",
)
async def list_active_orders(
    principal: OrderReaderDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
) -> list[OrderResponse]:
    active = await ListActiveOrders(orders)(principal)
    views = await DescribeOrders(tables, staff)(principal.restaurant_id, active)
    return [OrderResponse.from_view(view) for view in views]


@router.post(
    "",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Abrir un pedido en mesa o para llevar",
)
async def open_order(
    payload: OpenOrderRequest,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    menu: MenuCatalogDep,
    timezones: RestaurantTimezoneReaderDep,
    staff: StaffDirectoryDep,
    events: EventPublisherDep,
    customers: CustomerDirectoryDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await OpenOrder(
            orders, tables, menu, timezones, events, activity, customers=customers, clock=clock
        )(
            OpenOrderCommand(
                actor=principal,
                type=payload.type,
                table_id=payload.table_id,
                customer_name=payload.customer_name,
                customer_phone=payload.customer_phone,
                delivery_address=payload.delivery_address,
                delivery_reference=payload.delivery_reference,
                customer_id=payload.customer_id,
                client_request_id=payload.client_request_id,
                notes=payload.notes,
                items=_new_items(payload.items),
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.get("/{order_id}", response_model=OrderResponse, summary="Ver un pedido")
async def read_order(
    order_id: PathId,
    principal: OrderReaderDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
) -> OrderResponse:
    try:
        order = await ReadOrder(orders)(principal, order_id)
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.patch(
    "/{order_id}", response_model=OrderResponse, summary="Editar la nota o el nombre del cliente"
)
async def update_order(
    order_id: PathId,
    payload: UpdateOrderRequest,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await UpdateOrderDetails(orders, events, clock=clock)(
            UpdateOrderDetailsCommand(
                actor=principal,
                order_id=order_id,
                notes=payload.notes,
                customer_name=payload.customer_name,
                delivery=_delivery(payload),
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post(
    "/{order_id}/items",
    response_model=OrderResponse,
    summary="Agregar platos (si ya estaba listo o servido, vuelve a cocina)",
)
async def add_items(
    order_id: PathId,
    payload: AddItemsRequest,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    menu: MenuCatalogDep,
    staff: StaffDirectoryDep,
    events: EventPublisherDep,
    kitchen_hook: SentToKitchenHookDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await AddItems(orders, menu, events, kitchen_hook, activity, clock=clock)(
            AddItemsCommand(actor=principal, order_id=order_id, items=_new_items(payload.items))
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.patch(
    "/{order_id}/items/{item_id}",
    response_model=OrderResponse,
    summary="Cambiar cantidad o nota de un plato (solo con el pedido abierto)",
)
async def change_item(
    order_id: PathId,
    item_id: PathId,
    payload: ChangeItemRequest,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await ChangeItem(orders, events, clock=clock)(
            ChangeItemCommand(
                actor=principal,
                order_id=order_id,
                item_id=item_id,
                quantity=payload.quantity,
                notes=payload.notes,
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.delete(
    "/{order_id}/items/{item_id}",
    response_model=OrderResponse,
    summary="Quitar un plato (solo con el pedido abierto)",
)
async def remove_item(
    order_id: PathId,
    item_id: PathId,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await RemoveItem(orders, events, clock=clock)(
            RemoveItemCommand(actor=principal, order_id=order_id, item_id=item_id)
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post(
    "/{order_id}/items/{item_id}/void",
    response_model=OrderResponse,
    summary="Anular un plato ya enviado a cocina (antes del primer pago)",
)
async def void_item(
    order_id: PathId,
    item_id: PathId,
    payload: VoidItemRequest,
    principal: KitchenManagerDep,
    orders: OrderRepositoryDep,
    consumption: ConsumedItemsReaderDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await VoidItem(orders, consumption, activity, events, clock=clock)(
            VoidItemCommand(
                actor=principal, order_id=order_id, item_id=item_id, reason=payload.reason
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post(
    "/{order_id}/payments/{payment_id}/void",
    response_model=OrderResponse,
    summary="Anular un cobro del turno de caja abierto, con su motivo",
)
async def void_payment(
    order_id: PathId,
    payment_id: PathId,
    payload: VoidPaymentRequest,
    principal: CashManagerDep,
    orders: OrderRepositoryDep,
    cash: CashRegisterDep,
    invoices: InvoiceStatusReaderDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    """RFS-25: un cobro mal hecho se anula, no se borra: queda con quién y por qué.

    El pedido vuelve a deber ese monto. 409 si el turno de caja del cobro ya se
    cerró (`payment_not_voidable`) o si el pedido tiene comprobante vigente
    (`order_already_invoiced`).
    """
    try:
        order = await VoidPayment(orders, cash, invoices, activity, events, clock=clock)(
            VoidPaymentCommand(
                actor=principal, order_id=order_id, payment_id=payment_id, reason=payload.reason
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post("/{order_id}/send", response_model=OrderResponse, summary="Enviar a cocina")
async def send_to_kitchen(
    order_id: PathId,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    events: EventPublisherDep,
    kitchen_hook: SentToKitchenHookDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await SendToKitchen(orders, events, kitchen_hook, clock=clock)(principal, order_id)
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post("/{order_id}/ready", response_model=OrderResponse, summary="Marcar como listo")
async def mark_ready(
    order_id: PathId,
    principal: KitchenManagerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await MarkReady(orders, events, clock=clock)(principal, order_id)
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post(
    "/{order_id}/served",
    response_model=OrderResponse,
    summary="Marcar como servido (descuenta el inventario según las recetas)",
)
async def mark_served(
    order_id: PathId,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    served_hook: ServedOrderHookDep,
    events: EventPublisherDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await MarkServed(orders, served_hook, events, activity, clock=clock)(
            principal, order_id
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post("/{order_id}/charge", response_model=OrderResponse, summary="Cobrar lo que falta")
async def charge_order(
    order_id: PathId,
    payload: ChargeOrderRequest,
    principal: CashierDep,
    orders: OrderRepositoryDep,
    cash: CashRegisterDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await ChargeOrder(orders, cash, activity, events, clock=clock)(
            ChargeOrderCommand(
                actor=principal,
                order_id=order_id,
                payment_method=payload.payment_method,
                amount_received=payload.amount_received,
                tip=payload.tip,
                expected_balance=payload.expected_balance,
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post(
    "/{order_id}/payments",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar un pago: la cuenta, una parte o los platos de alguien",
)
async def add_payment(
    order_id: PathId,
    payload: PaymentRequest,
    principal: CashierDep,
    orders: OrderRepositoryDep,
    cash: CashRegisterDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await ChargeOrder(orders, cash, activity, events, clock=clock)(
            ChargeOrderCommand(
                actor=principal,
                order_id=order_id,
                payment_method=payload.payment_method,
                amount_received=payload.amount_received,
                tip=payload.tip,
                amount=payload.amount,
                item_ids=tuple(payload.item_ids),
                expected_balance=payload.expected_balance,
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.put("/{order_id}/discount", response_model=OrderResponse, summary="Aplicar un descuento")
async def apply_discount(
    order_id: PathId,
    payload: DiscountRequest,
    principal: CashierDep,
    orders: OrderRepositoryDep,
    policy: DiscountPolicyDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await ApplyDiscount(orders, policy, activity, events, clock=clock)(
            ApplyDiscountCommand(
                actor=principal, order_id=order_id, percent=payload.percent, reason=payload.reason
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.put(
    "/{order_id}/items/{item_id}/courtesy",
    response_model=OrderResponse,
    summary="Invitar un plato (cortesía)",
)
async def grant_courtesy(
    order_id: PathId,
    item_id: PathId,
    payload: CourtesyRequest,
    principal: DiscountManagerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await SetCourtesy(orders, activity, events, clock=clock)(
            CourtesyCommand(
                actor=principal, order_id=order_id, item_id=item_id, reason=payload.reason
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.delete(
    "/{order_id}/items/{item_id}/courtesy",
    response_model=OrderResponse,
    summary="Dejar de invitar un plato",
)
async def revoke_courtesy(
    order_id: PathId,
    item_id: PathId,
    principal: DiscountManagerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await SetCourtesy(orders, activity, events, clock=clock)(
            CourtesyCommand(actor=principal, order_id=order_id, item_id=item_id, reason=None)
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post("/{order_id}/move", response_model=OrderResponse, summary="Cambiar de mesa")
async def move_order(
    order_id: PathId,
    payload: MoveOrderRequest,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    timezones: RestaurantTimezoneReaderDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await MoveOrder(orders, tables, timezones, activity, events, clock=clock)(
            MoveOrderCommand(actor=principal, order_id=order_id, table_id=payload.table_id)
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post("/{order_id}/merge", response_model=OrderResponse, summary="Unir otra mesa a esta")
async def merge_orders(
    order_id: PathId,
    payload: MergeOrdersRequest,
    principal: OrderTakerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await MergeOrders(orders, activity, events, clock=clock)(
            MergeOrdersCommand(
                actor=principal, order_id=order_id, source_order_id=payload.source_order_id
            )
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)


@router.post("/{order_id}/cancel", response_model=OrderResponse, summary="Cancelar con motivo")
async def cancel_order(
    order_id: PathId,
    payload: CancelOrderRequest,
    principal: KitchenManagerDep,
    orders: OrderRepositoryDep,
    tables: TableRepositoryDep,
    staff: StaffDirectoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> OrderResponse:
    try:
        order = await CancelOrder(orders, activity, events, clock=clock)(
            CancelOrderCommand(actor=principal, order_id=order_id, reason=payload.reason)
        )
    except OrdersError as error:
        raise http_error(error) from error
    return await _respond(principal, order, tables, staff)
