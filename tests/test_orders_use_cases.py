"""Casos de uso de pedidos, mesas y caja con dobles en memoria.

Cada caso afirma lo que queda guardado, el asiento de bitácora con su texto,
a quién avisa el tablero en vivo, qué pedido queda tomado (`for_update`) y qué
le llega a la cocina o al inventario.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.identity import Principal
from resthub.core.pagination import Page
from resthub.core.permissions import Permission
from resthub.modules.orders.domain.cash import CashOrderAdjustment, CashPayment, CashSession
from resthub.modules.orders.domain.exceptions import (
    CashRegisterAlreadyOpenError,
    CashRegisterClosedError,
    CashSessionNotFoundError,
    CustomerNotFoundError,
    DiscountNotAllowedError,
    DishNotFoundError,
    DishUnavailableError,
    InvalidOrderError,
    InvalidTransitionError,
    NotYourOrderError,
    OrderNotFoundError,
    TableInactiveError,
    TableLabelTakenError,
    TableNotFoundError,
    TableOccupiedError,
)
from resthub.modules.orders.domain.modifiers import DishOption, DishOptionGroup
from resthub.modules.orders.domain.orders import (
    Order,
    OrderStatus,
    OrderType,
    PaymentMethod,
)
from resthub.modules.orders.domain.tables import DiningTable, TableStatus
from resthub.modules.orders.ports.customer_directory import KnownCustomer
from resthub.modules.orders.ports.menu_catalog import OrderableDish
from resthub.modules.orders.ports.order_repository import OrderQuery
from resthub.modules.orders.ports.sent_to_kitchen_hook import SentOrder
from resthub.modules.orders.ports.served_order_hook import ServedOrder
from resthub.modules.orders.use_cases.adjustments import (
    ApplyDiscount,
    ApplyDiscountCommand,
    CourtesyCommand,
    SetCourtesy,
)
from resthub.modules.orders.use_cases.cash import (
    CASH_TOPIC,
    CloseCash,
    CloseCashCommand,
    DescribeCash,
    ListCashSessions,
    OpenCash,
    OpenCashCommand,
    ReadCashSession,
    ReadCurrentCash,
)
from resthub.modules.orders.use_cases.charge_order import ChargeOrder, ChargeOrderCommand
from resthub.modules.orders.use_cases.kitchen import CancelOrder, CancelOrderCommand, MarkReady
from resthub.modules.orders.use_cases.manage_tables import (
    CreateTable,
    CreateTableCommand,
    ListTables,
    ListTablesQuery,
    ReorderTables,
    ReorderTablesCommand,
    TableView,
    UpdateTable,
    UpdateTableCommand,
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
from resthub.modules.orders.use_cases.shared import ORDERS_TOPIC, DescribeOrders
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
from tests.conftest import RecordingActivity
from tests.fakes import RecordingEvents

RESTAURANT = 1
OTHER_RESTAURANT = 2
D = Decimal
TAKE = frozenset({Permission.ORDERS_TAKE.value, Permission.ORDERS_CHARGE.value})
ALL = frozenset(p.value for p in Permission)
WAITER = Principal(user_id=7, role_id=2, is_active=True, restaurant_id=RESTAURANT, permissions=TAKE)
OTHER_WAITER = replace(WAITER, user_id=8)
OWNER = Principal(user_id=9, role_id=1, is_active=True, restaurant_id=RESTAURANT, permissions=ALL)


# -- Dobles --------------------------------------------------------------------------------


class FakeOrders:
    def __init__(self) -> None:
        self.rows: dict[int, Order] = {}
        self.locks: list[int] = []
        self.queries: list[OrderQuery] = []
        self.merges: list[tuple[int, int]] = []

    async def add(self, order: Order) -> Order:
        order.id = len(self.rows) + 1
        for i, item in enumerate(order.items):
            item.id = 100 * order.id + i
        self.rows[order.id] = order
        return order

    async def get(self, restaurant_id: int, order_id: int, *, for_update: bool = False):
        if for_update:
            self.locks.append(order_id)
        found = self.rows.get(order_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def by_client_request(self, restaurant_id: int, client_request_id: str):
        return next(
            (
                o
                for o in self.rows.values()
                if o.restaurant_id == restaurant_id and o.client_request_id == client_request_id
            ),
            None,
        )

    async def save(self, order: Order) -> Order:
        for i, item in enumerate(order.items):
            if item.id is None:
                item.id = 100 * (order.id or 0) + 50 + i
        self.rows[order.id or 0] = order
        return order

    async def save_merge(self, target: Order, source: Order) -> Order:
        self.merges.append((target.id or 0, source.id or 0))
        self.rows[source.id or 0] = source
        return await self.save(target)

    async def search(self, query: OrderQuery) -> Page[Order]:
        self.queries.append(query)
        items = [o for o in self.rows.values() if o.restaurant_id == query.restaurant_id]
        return Page(items=items, total=len(items))

    async def list_active(self, restaurant_id: int) -> list[Order]:
        return [o for o in self.rows.values() if o.restaurant_id == restaurant_id and o.is_active]

    async def active_for_table(self, restaurant_id: int, table_id: int):
        return next(
            (o for o in await self.list_active(restaurant_id) if o.table_id == table_id),
            None,
        )

    async def last_number(self, restaurant_id: int, business_date: date) -> int:
        return max(
            (
                o.number
                for o in self.rows.values()
                if o.restaurant_id == restaurant_id and o.business_date == business_date
            ),
            default=0,
        )


class FakeTables:
    def __init__(self) -> None:
        self.rows: dict[int, DiningTable] = {}
        self.positions: list[list[int]] = []

    async def add(self, table: DiningTable) -> DiningTable:
        stored = replace(table, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return replace(stored)

    async def get(self, restaurant_id: int, table_id: int) -> DiningTable | None:
        found = self.rows.get(table_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def find_by_label(self, restaurant_id: int, label: str) -> DiningTable | None:
        return next(
            (
                replace(t)
                for t in self.rows.values()
                if t.restaurant_id == restaurant_id and t.label.casefold() == label.casefold()
            ),
            None,
        )

    async def list_all(self, restaurant_id: int) -> list[DiningTable]:
        return sorted(
            (replace(t) for t in self.rows.values() if t.restaurant_id == restaurant_id),
            key=lambda t: t.position,
        )

    async def save(self, table: DiningTable) -> DiningTable:
        self.rows[table.id or 0] = replace(table)
        return replace(table)

    async def save_positions(self, tables: list[DiningTable]) -> None:
        self.positions.append([t.id or 0 for t in tables])
        for t in tables:
            self.rows[t.id or 0] = replace(t)


class FakeMenu:
    def __init__(self, *dishes: OrderableDish) -> None:
        self.dishes = {p.id: p for p in dishes}

    async def get_dishes(self, restaurant_id: int, dish_ids: Collection[int]):
        return {i: p for i, p in self.dishes.items() if i in dish_ids}


class FakeNumberingClock:
    def __init__(self) -> None:
        self.calls = 0

    async def timezone_for_numbering(self, restaurant_id: int) -> str:
        self.calls += 1
        return "America/Lima"


class FakeStaff:
    def __init__(self, **user_names: str) -> None:
        self.user_names = {int(k.removeprefix("u")): v for k, v in user_names.items()}
        self.lookups: list[set[int]] = []

    async def names(self, restaurant_id: int, user_ids: Collection[int]) -> dict[int, str]:
        self.lookups.append(set(user_ids))
        return {i: n for i, n in self.user_names.items() if i in user_ids}


class CustomerBook:
    def __init__(self, *customers: KnownCustomer) -> None:
        self.customers = list(customers)

    async def get(self, restaurant_id: int, customer_id: int) -> KnownCustomer | None:
        return next((c for c in self.customers if c.id == customer_id), None)

    async def by_phone(self, restaurant_id: int, phone: str) -> KnownCustomer | None:
        return next((c for c in self.customers if phone and c.phone == phone), None)


class FakeKitchen:
    def __init__(self) -> None:
        self.sent_orders: list[SentOrder] = []
        self.served_orders: list[ServedOrder] = []

    def order_sent(self, sent: SentOrder) -> None:
        self.sent_orders.append(sent)

    async def order_served(self, served: ServedOrder) -> None:
        self.served_orders.append(served)


class FakeCashRegister:
    def __init__(self, open_session: CashSession | None = None) -> None:
        self.current_session = open_session
        self.sessions: dict[int, CashSession] = {}
        self.locks = 0
        self.recorded_payments: list[CashPayment] = []
        self.recorded_adjustments: list[CashOrderAdjustment] = []
        if open_session is not None:
            open_session.id = open_session.id or 1
            self.sessions[open_session.id] = open_session

    async def current(self, restaurant_id: int, *, for_update: bool = False):
        if for_update:
            self.locks += 1
        if (
            self.current_session
            and self.current_session.restaurant_id == restaurant_id
            and self.current_session.is_open
        ):
            return self.current_session
        return None

    async def get(self, restaurant_id: int, session_id: int) -> CashSession | None:
        found = self.sessions.get(session_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def open(self, session: CashSession) -> CashSession:
        session.id = len(self.sessions) + 1
        self.sessions[session.id] = session
        self.current_session = session
        return session

    async def save(self, session: CashSession) -> CashSession:
        self.sessions[session.id or 0] = session
        return session

    async def history(self, restaurant_id: int, limit: int, offset: int) -> Page[CashSession]:
        items = [t for t in self.sessions.values() if t.restaurant_id == restaurant_id]
        return Page(items=items[offset : offset + limit], total=len(items))

    async def payments(self, restaurant_id: int, session_id: int) -> list[CashPayment]:
        return list(self.recorded_payments)

    async def adjustments(self, restaurant_id: int, session_id: int) -> list[CashOrderAdjustment]:
        return list(self.recorded_adjustments)


class DiscountCap:
    def __init__(self, limit: str = "10") -> None:
        self.limit = D(limit)
        self.queries = 0

    async def waiter_limit(self, restaurant_id: int) -> Decimal:
        self.queries += 1
        return self.limit


LOMO = OrderableDish(1, "Lomo saltado", D("28.00"), True, True)
PIZZA = OrderableDish(
    2,
    "Pizza",
    D("30.00"),
    True,
    True,
    modifier_groups=(
        DishOptionGroup(
            "Tamaño", (DishOption("Personal", D("0")), DishOption("Familiar", D("12.50"))), 1
        ),
    ),
)
SOLD_OUT = OrderableDish(3, "Ceviche", D("30"), True, False)
OUT_OF_STOCK = OrderableDish(4, "Causa", D("15"), True, True, out_of_stock=True)
INACTIVE_DISH = OrderableDish(5, "Seco", D("25"), False, True)


class DiningRoom:
    def __init__(self) -> None:
        self.orders = FakeOrders()
        self.tables = FakeTables()
        self.menu = FakeMenu(LOMO, PIZZA, SOLD_OUT, OUT_OF_STOCK, INACTIVE_DISH)
        self.clock = FakeNumberingClock()
        self.events = RecordingEvents()
        self.activity = RecordingActivity()
        self.kitchen = FakeKitchen()

    async def table(self, label: str = "1", **extra: object) -> DiningTable:
        return await self.tables.add(DiningTable(RESTAURANT, label, **extra))  # type: ignore[arg-type]

    def open_order(self, customers: CustomerBook | None = None) -> OpenOrder:
        return OpenOrder(self.orders, self.tables, self.menu, self.clock, self.events, customers)

    async def order(
        self, actor: Principal = WAITER, table_id: int | None = None, **extra: object
    ) -> Order:
        if table_id is None and "type" not in extra:
            table_id = (await self.table(f"M{len(self.tables.rows) + 1}")).id
        command = OpenOrderCommand(
            actor=actor,
            type=extra.pop("type", OrderType.DINE_IN),  # type: ignore[arg-type]
            table_id=table_id,
            items=extra.pop("items", (NewItem(LOMO.id),)),  # type: ignore[arg-type]
            **extra,  # type: ignore[arg-type]
        )
        return await self.open_order()(command)

    async def served(self, actor: Principal = WAITER, **extra: object) -> Order:
        order = await self.order(actor, **extra)
        await SendToKitchen(self.orders, self.events, self.kitchen)(actor, order.id or 0)
        await MarkReady(self.orders, self.events)(OWNER, order.id or 0)
        await MarkServed(self.orders, self.kitchen, self.events)(actor, order.id or 0)
        self.orders.locks.clear()
        self.events.published.clear()
        return order

    def last_event(self) -> tuple[int, str, frozenset[int], bool, int | None]:
        e = self.events.published[-1]
        return (e.restaurant_id, e.topic, e.user_ids, e.everyone, e.reference_id)


# -- Abrir pedidos ------------------------------------------------------------------------


async def test_opening_a_dine_in_order_freezes_dishes_and_notifies_the_restaurant() -> None:
    s = DiningRoom()
    table = await s.table()

    order = await s.open_order()(
        OpenOrderCommand(
            WAITER,
            OrderType.DINE_IN,
            table_id=table.id,
            customer_name="Ana",
            notes="sin ají",
            items=(
                NewItem(LOMO.id, quantity=2, notes="bien cocido"),
                NewItem(PIZZA.id, modifiers=(("tamaño", "familiar"),)),
            ),
        )
    )

    assert (order.number, order.waiter_id, order.table_id, order.status) == (
        1,
        WAITER.user_id,
        table.id,
        OrderStatus.OPEN,
    )
    assert (order.customer_name, order.notes) == ("Ana", "sin ají")
    assert [(i.name, i.quantity, i.unit_price, i.notes) for i in order.items] == [
        ("Lomo saltado", 2, D("28.00"), "bien cocido"),
        ("Pizza", 1, D("42.50"), ""),
    ]
    assert [(m.group, m.option) for m in order.items[1].modifiers] == [("Tamaño", "Familiar")]
    assert order.total == D("98.50")
    assert s.clock.calls == 1
    assert s.last_event() == (RESTAURANT, ORDERS_TOPIC, frozenset({7}), True, order.id)


async def test_numbering_follows_the_restaurant_day() -> None:
    s = DiningRoom()

    first = await s.order()
    second = await s.order(type=OrderType.TAKEAWAY)

    assert (first.number, second.number) == (1, 2)
    assert second.table_id is None


async def test_retry_with_the_same_request_id_returns_the_same_order() -> None:
    s = DiningRoom()
    table = await s.table()
    command = OpenOrderCommand(
        WAITER, OrderType.DINE_IN, table_id=table.id, client_request_id="abc-1"
    )

    first = await s.open_order()(command)
    second = await s.open_order()(command)

    assert second is first
    assert len(s.orders.rows) == 1
    assert len(s.events.published) == 1


@pytest.mark.parametrize(
    ("items", "error", "message"),
    [
        ((NewItem(99),), DishNotFoundError, "No existe el plato 99."),
        ((NewItem(SOLD_OUT.id),), DishUnavailableError, "Ceviche no está disponible hoy."),
        ((NewItem(OUT_OF_STOCK.id),), DishUnavailableError, "Causa no está disponible hoy."),
        ((NewItem(INACTIVE_DISH.id),), DishUnavailableError, "Seco no está disponible hoy."),
        ((NewItem(PIZZA.id),), InvalidOrderError, "Elige tamaño para Pizza."),
    ],
)
async def test_only_what_is_on_the_menu_and_orderable_gets_in(
    items: tuple[NewItem, ...], error: type[Exception], message: str
) -> None:
    s = DiningRoom()

    with pytest.raises(error) as failure:
        await s.order(items=items)
    assert str(failure.value) == message
    assert s.orders.rows == {}


async def test_inactive_or_occupied_table_does_not_receive_another_order() -> None:
    s = DiningRoom()
    inactive = await s.table("Terraza", is_active=False)
    occupied = await s.table("2")
    first = await s.order(table_id=occupied.id)

    with pytest.raises(TableInactiveError) as inactive_error:
        await s.order(table_id=inactive.id)
    with pytest.raises(TableOccupiedError) as occupied_error:
        await s.order(table_id=occupied.id)
    with pytest.raises(TableNotFoundError):
        await s.order(table_id=999)
    with pytest.raises(InvalidOrderError) as no_table:
        await s.open_order()(OpenOrderCommand(WAITER, OrderType.DINE_IN))

    assert str(inactive_error.value) == "La mesa Terraza está desactivada."
    assert (occupied_error.value.label, occupied_error.value.order_id) == ("2", first.id)
    assert str(no_table.value) == "Un pedido en mesa necesita la mesa."
    assert len(s.orders.rows) == 1


async def test_customer_from_the_book_fills_in_what_is_missing() -> None:
    s = DiningRoom()
    ana = KnownCustomer(5, "Ana", "987654321", "Jr. Pizarro 450", "portón verde")

    order = await s.open_order(CustomerBook(ana))(
        OpenOrderCommand(WAITER, OrderType.DELIVERY, customer_id=5, delivery_reference="timbre 2")
    )

    assert (
        order.customer_name,
        order.customer_phone,
        order.delivery_address,
        order.delivery_reference,
        order.customer_id,
    ) == ("Ana", "987654321", "Jr. Pizarro 450", "timbre 2", 5)


async def test_what_the_waiter_wrote_overrides_the_book() -> None:
    s = DiningRoom()
    ana = KnownCustomer(5, "Ana", "987654321", "Jr. Pizarro 450", "portón verde")

    order = await s.open_order(CustomerBook(ana))(
        OpenOrderCommand(
            WAITER,
            OrderType.DELIVERY,
            customer_id=5,
            customer_name="Ana T.",
            customer_phone="911",
            delivery_address="Av. Grau 1",
        )
    )

    assert (order.customer_name, order.customer_phone, order.delivery_address) == (
        "Ana T.",
        "911",
        "Av. Grau 1",
    )
    assert order.delivery_reference == "portón verde"


async def test_known_phone_identifies_the_customer_and_a_foreign_one_does_not() -> None:
    s = DiningRoom()
    ana = KnownCustomer(5, "Ana", "987654321", "Jr. Pizarro 450", "")
    open_order = s.open_order(CustomerBook(ana))

    known = await open_order(
        OpenOrderCommand(
            WAITER,
            OrderType.DELIVERY,
            customer_name="A",
            customer_phone="987654321",
            delivery_address="x",
        )
    )
    new = await open_order(
        OpenOrderCommand(
            WAITER,
            OrderType.DELIVERY,
            customer_name="B",
            customer_phone="911",
            delivery_address="y",
        )
    )
    with pytest.raises(CustomerNotFoundError) as foreign:
        await open_order(OpenOrderCommand(WAITER, OrderType.TAKEAWAY, customer_id=77))

    assert (known.customer_id, known.customer_name) == (5, "A")
    assert new.customer_id is None
    assert foreign.value.customer_id == 77


async def test_without_a_book_the_customer_stays_as_sent() -> None:
    s = DiningRoom()

    order = await s.open_order()(OpenOrderCommand(WAITER, OrderType.TAKEAWAY, customer_id=3))

    assert order.customer_id == 3


# -- Platos y estados ---------------------------------------------------------------------------


async def test_adding_dishes_in_kitchen_sends_them_straight_to_preparing() -> None:
    s = DiningRoom()
    order = await s.order()
    await SendToKitchen(s.orders, s.events, s.kitchen)(WAITER, order.id or 0)
    s.kitchen.sent_orders.clear()

    saved = await AddItems(s.orders, s.menu, s.events, s.kitchen)(
        AddItemsCommand(WAITER, order.id or 0, (NewItem(LOMO.id, notes="para llevar"),))
    )

    assert saved.status is OrderStatus.IN_KITCHEN
    assert len(saved.items) == 2
    (sent_order,) = s.kitchen.sent_orders
    assert (sent_order.restaurant_id, sent_order.order_id, len(sent_order.items)) == (
        RESTAURANT,
        order.id,
        2,
    )
    assert sent_order.items[1].notes == "para llevar"
    assert s.orders.locks[-1] == order.id
    assert s.last_event()[4] == order.id


async def test_adding_to_an_open_order_does_not_notify_the_kitchen() -> None:
    s = DiningRoom()
    order = await s.order()

    await AddItems(s.orders, s.menu, s.events, s.kitchen)(
        AddItemsCommand(WAITER, order.id or 0, (NewItem(LOMO.id),))
    )

    assert s.kitchen.sent_orders == []
    assert len(s.events.published) == 2


async def test_closed_order_is_rejected_before_looking_at_the_menu() -> None:
    s = DiningRoom()
    order = await s.order()
    await CancelOrder(s.orders, s.activity, s.events)(
        CancelOrderCommand(WAITER, order.id or 0, "Se fueron")
    )

    with pytest.raises(InvalidTransitionError):
        await AddItems(s.orders, s.menu, s.events, s.kitchen)(
            AddItemsCommand(WAITER, order.id or 0, (NewItem(99),))
        )


async def test_changing_and_removing_dishes_save_and_notify() -> None:
    s = DiningRoom()
    order = await s.order(items=(NewItem(LOMO.id), NewItem(LOMO.id)))
    first, second = (i.id or 0 for i in order.items)

    changed = await ChangeItem(s.orders, s.events)(
        ChangeItemCommand(WAITER, order.id or 0, first, quantity=3, notes="sin sal")
    )
    removed = await RemoveItem(s.orders, s.events)(RemoveItemCommand(WAITER, order.id or 0, second))

    assert [(i.id, i.quantity, i.notes) for i in changed.items] == [(first, 3, "sin sal")]
    assert removed.item_count == 3
    assert s.orders.locks[-2:] == [order.id, order.id]
    assert len(s.events.published) == 3


async def test_editing_order_details_reaches_the_domain() -> None:
    s = DiningRoom()
    order = await s.order(
        type=OrderType.DELIVERY,
        customer_name="Ana",
        customer_phone="987",
        delivery_address="Jr. Pizarro 450",
    )

    saved = await UpdateOrderDetails(s.orders, s.events)(
        UpdateOrderDetailsCommand(
            WAITER,
            order.id or 0,
            notes="tocar timbre",
            customer_name="Ana T.",
            delivery=("911", None, "portón"),
        )
    )

    assert (saved.notes, saved.customer_name, saved.customer_phone) == (
        "tocar timbre",
        "Ana T.",
        "911",
    )
    assert (saved.delivery_address, saved.delivery_reference) == ("Jr. Pizarro 450", "portón")
    assert s.orders.locks[-1] == order.id
    assert s.last_event()[4] == order.id


async def test_serving_notifies_inventory_with_each_portion() -> None:
    s = DiningRoom()
    order = await s.order(
        items=(NewItem(LOMO.id, quantity=2), NewItem(PIZZA.id, modifiers=(("Tamaño", "Personal"),)))
    )
    await SendToKitchen(s.orders, s.events, s.kitchen)(WAITER, order.id or 0)
    await MarkReady(s.orders, s.events)(OWNER, order.id or 0)

    served = await MarkServed(s.orders, s.kitchen, s.events)(WAITER, order.id or 0)

    assert served.status is OrderStatus.SERVED
    (event,) = s.kitchen.served_orders
    assert (event.restaurant_id, event.order_id, event.actor_id) == (RESTAURANT, order.id, 7)
    assert [(p.order_item_id, p.menu_item_id, p.quantity) for p in event.portions] == [
        (order.items[0].id, LOMO.id, 2),
        (order.items[1].id, PIZZA.id, 1),
    ]
    (sent_order,) = s.kitchen.sent_orders
    assert [(k.menu_item_id, k.name) for k in sent_order.items] == [
        (LOMO.id, "Lomo saltado"),
        (PIZZA.id, "Pizza"),
    ]
    assert s.orders.locks == [order.id, order.id, order.id]


async def test_cancelling_logs_the_total_and_the_reason() -> None:
    s = DiningRoom()
    order = await s.order()

    cancelled = await CancelOrder(s.orders, s.activity, s.events)(
        CancelOrderCommand(OWNER, order.id or 0, "  Se  fueron ")
    )

    assert cancelled.status is OrderStatus.CANCELLED
    assert s.activity.entries == [
        (RESTAURANT, OWNER.user_id, ActivityKind.ORDER_CANCELLED, "Pedido #1 (S/ 28.00): Se fueron")
    ]
    assert s.last_event()[4] == order.id
    assert s.orders.locks == [order.id]


# -- Lo que ve cada quien ---------------------------------------------------------------------


async def test_waiter_does_not_see_anothers_closed_orders_but_sees_active_ones() -> None:
    s = DiningRoom()
    active = await s.order(OTHER_WAITER)
    closed = await s.order(OTHER_WAITER)
    await CancelOrder(s.orders, s.activity, s.events)(
        CancelOrderCommand(OWNER, closed.id or 0, "x")
    )
    read = ReadOrder(s.orders)

    assert (await read(WAITER, active.id or 0)).id == active.id
    assert (await read(OWNER, closed.id or 0)).id == closed.id
    assert (await read(OTHER_WAITER, closed.id or 0)).id == closed.id
    with pytest.raises(OrderNotFoundError) as hidden:
        await read(WAITER, closed.id or 0)
    with pytest.raises(OrderNotFoundError):
        await read(replace(OWNER, restaurant_id=OTHER_RESTAURANT), active.id or 0)
    assert hidden.value.order_id == closed.id


async def test_waiter_history_is_limited_to_their_own() -> None:
    s = DiningRoom()
    list_all = ListOrders(s.orders)
    filters = {
        "statuses": frozenset({OrderStatus.PAID}),
        "date_from": date(2026, 9, 1),
        "date_to": date(2026, 9, 30),
        "type": OrderType.TAKEAWAY,
        "table_id": 3,
        "waiter_id": 8,
        "limit": 5,
        "offset": 10,
    }

    await list_all(ListOrdersQuery(WAITER, **filters))  # type: ignore[arg-type]
    await list_all(ListOrdersQuery(OWNER))

    assert s.orders.queries == [
        OrderQuery(restaurant_id=RESTAURANT, visible_to=WAITER.user_id, **filters),  # type: ignore[arg-type]
        OrderQuery(restaurant_id=RESTAURANT, visible_to=None),
    ]


async def test_board_shows_all_active_orders() -> None:
    s = DiningRoom()
    mine = await s.order(WAITER)
    foreign = await s.order(OTHER_WAITER)

    active_orders = await ListActiveOrders(s.orders)(WAITER)

    assert [o.id for o in active_orders] == [mine.id, foreign.id]


async def test_describe_orders_adds_table_and_names_in_one_query() -> None:
    s = DiningRoom()
    table = await s.table("Terraza 2")
    dine_in = await s.order(WAITER, table_id=table.id)
    takeaway = await s.order(OTHER_WAITER, type=OrderType.TAKEAWAY)
    dine_in.discounted_by = 9
    staff = FakeStaff(u7="Luis", u9="Rosa")

    views = await DescribeOrders(s.tables, staff)(RESTAURANT, [dine_in, takeaway])

    assert [(v.table_label, v.waiter_name) for v in views] == [("Terraza 2", "Luis"), (None, "")]
    assert staff.lookups == [{7, 8, 9}]
    assert views[0].staff_names == {7: "Luis", 9: "Rosa"}
    assert await DescribeOrders(s.tables, staff)(RESTAURANT, []) == []
    assert (await DescribeOrders(s.tables, staff).one(RESTAURANT, takeaway)).order is takeaway


# -- Mover y unir ------------------------------------------------------------------------------


async def test_moving_an_order_to_another_table_logs_from_where_to_where() -> None:
    s = DiningRoom()
    first = await s.table("1")
    second = await s.table("Barra")
    order = await s.order(table_id=first.id)
    move = MoveOrder(s.orders, s.tables, s.clock, s.activity, s.events)

    moved = await move(MoveOrderCommand(WAITER, order.id or 0, second.id or 0))

    assert moved.table_id == second.id
    assert s.activity.entries == [
        (RESTAURANT, WAITER.user_id, ActivityKind.ORDER_MOVED, "Pedido #1: de 1 a Barra")
    ]
    assert s.orders.locks == [order.id]
    assert s.clock.calls == 2
    assert s.last_event()[4] == order.id


async def test_cannot_move_to_an_occupied_inactive_or_foreign_table() -> None:
    s = DiningRoom()
    first = await s.table("1")
    second = await s.table("2")
    inactive = await s.table("Terraza", is_active=False)
    order = await s.order(table_id=first.id)
    other = await s.order(table_id=second.id)
    move = MoveOrder(s.orders, s.tables, s.clock, s.activity, s.events)

    with pytest.raises(TableOccupiedError) as occupied:
        await move(MoveOrderCommand(WAITER, order.id or 0, second.id or 0))
    with pytest.raises(TableInactiveError):
        await move(MoveOrderCommand(WAITER, order.id or 0, inactive.id or 0))
    with pytest.raises(TableNotFoundError):
        await move(MoveOrderCommand(WAITER, order.id or 0, 999))
    with pytest.raises(InvalidOrderError):
        await move(MoveOrderCommand(WAITER, order.id or 0, first.id or 0))

    assert occupied.value.order_id == other.id
    assert s.orders.rows[order.id or 0].table_id == first.id
    assert s.activity.entries == []


async def test_moving_from_a_table_that_no_longer_exists_logs_a_question_mark() -> None:
    s = DiningRoom()
    first = await s.table("1")
    second = await s.table("2")
    order = await s.order(table_id=first.id)
    del s.tables.rows[first.id or 0]

    await MoveOrder(s.orders, s.tables, s.clock, s.activity, s.events)(
        MoveOrderCommand(WAITER, order.id or 0, second.id or 0)
    )

    assert s.activity.entries[-1][3] == "Pedido #1: de ? a 2"


async def test_merging_tables_locks_both_in_order_and_notifies_for_both() -> None:
    s = DiningRoom()
    kept = await s.order()
    merged_in = await s.order(items=(NewItem(LOMO.id), NewItem(LOMO.id)))
    merge = MergeOrders(s.orders, s.activity, s.events)

    merged = await merge(MergeOrdersCommand(WAITER, merged_in.id or 0, kept.id or 0))

    assert merged.id == merged_in.id
    assert len(merged.items) == 3
    assert s.orders.rows[kept.id or 0].status is OrderStatus.CANCELLED
    assert s.orders.locks == [kept.id, merged_in.id]
    assert s.orders.merges == [(merged_in.id, kept.id)]
    assert s.activity.entries == [
        (RESTAURANT, WAITER.user_id, ActivityKind.ORDER_MERGED, "Pedido #1 unido al #2 (S/ 84.00)")
    ]
    assert [e.reference_id for e in s.events.published[-2:]] == [kept.id, merged_in.id]


async def test_waiter_only_merges_their_own_tables() -> None:
    s = DiningRoom()
    own = await s.order(WAITER)
    foreign = await s.order(OTHER_WAITER)

    with pytest.raises(NotYourOrderError) as error:
        await MergeOrders(s.orders, s.activity, s.events)(
            MergeOrdersCommand(WAITER, own.id or 0, foreign.id or 0)
        )
    assert str(error.value) == (
        "Solo el mesero que tomó el pedido o el encargado pueden unir esas mesas."
    )
    merged = await MergeOrders(s.orders, s.activity, s.events)(
        MergeOrdersCommand(OWNER, own.id or 0, foreign.id or 0)
    )
    assert merged.id == own.id


# -- Descuentos y cortesías ----------------------------------------------------------------------


async def test_waiter_discounts_up_to_their_cap_and_it_is_logged() -> None:
    s = DiningRoom()
    order = await s.served()
    cap = DiscountCap("10")
    apply_discount = ApplyDiscount(s.orders, cap, s.activity, s.events)

    with pytest.raises(DiscountNotAllowedError):
        await apply_discount(ApplyDiscountCommand(WAITER, order.id or 0, D("15"), "amigo"))
    saved = await apply_discount(ApplyDiscountCommand(WAITER, order.id or 0, D("10"), "frecuente"))
    removed = await apply_discount(ApplyDiscountCommand(WAITER, order.id or 0, D("0")))

    assert cap.queries == 3
    assert saved.discounted_by is None and removed.discount_percent == 0
    assert [e[3] for e in s.activity.entries] == [
        "Pedido #1: 10.00 % (S/ 2.80), frecuente",
        "Pedido #1: quitó el descuento",
    ]
    assert s.activity.entries[0][:3] == (RESTAURANT, WAITER.user_id, ActivityKind.ORDER_DISCOUNTED)
    assert s.orders.locks == [order.id] * 3


async def test_owner_discounts_without_cap_and_waiter_cannot_touch_foreign_orders() -> None:
    s = DiningRoom()
    order = await s.served(OTHER_WAITER)
    cap = DiscountCap("5")

    saved = await ApplyDiscount(s.orders, cap, s.activity, s.events)(
        ApplyDiscountCommand(OWNER, order.id or 0, D("50"), "cumpleaños")
    )
    with pytest.raises(NotYourOrderError) as foreign:
        await ApplyDiscount(s.orders, cap, s.activity, s.events)(
            ApplyDiscountCommand(WAITER, order.id or 0, D("1"), "x")
        )

    assert (saved.discount_percent, saved.discounted_by) == (D("50.00"), OWNER.user_id)
    assert cap.queries == 0
    assert foreign.value.action == "darle un descuento"
    assert s.last_event()[4] == order.id


async def test_comping_and_uncomping_a_dish_is_logged() -> None:
    s = DiningRoom()
    order = await s.order(items=(NewItem(LOMO.id, quantity=2),))
    item = order.items[0].id or 0
    courtesy = SetCourtesy(s.orders, s.activity, s.events)

    comped = await courtesy(CourtesyCommand(OWNER, order.id or 0, item, "Error de cocina"))
    assert comped.total == D("0.00")
    charged = await courtesy(CourtesyCommand(OWNER, order.id or 0, item, None))

    assert charged.total == D("56.00")
    assert [e[2:] for e in s.activity.entries] == [
        (
            ActivityKind.ORDER_COURTESY,
            "Pedido #1: invitó 2 × Lomo saltado (S/ 56.00), Error de cocina",  # noqa: RUF001
        ),
        (ActivityKind.ORDER_COURTESY, "Pedido #1: Lomo saltado vuelve a cobrarse"),
    ]
    assert s.activity.entries[0][:2] == (RESTAURANT, OWNER.user_id)
    assert s.orders.locks[-2:] == [order.id, order.id]
    assert s.last_event()[4] == order.id


# -- Cobrar ---------------------------------------------------------------------------------------


def _cash_register() -> FakeCashRegister:
    return FakeCashRegister(CashSession(RESTAURANT, opened_by=9, opening_amount=D("100"), id=4))


async def test_charging_everything_at_once_lands_in_the_cash_register_and_is_logged() -> None:
    s = DiningRoom()
    order = await s.served()
    cash_register = _cash_register()

    paid = await ChargeOrder(s.orders, cash_register, s.activity, s.events)(
        ChargeOrderCommand(
            WAITER, order.id or 0, PaymentMethod.CASH, amount_received=D("50"), tip=D("2")
        )
    )

    assert paid.status is OrderStatus.PAID
    (payment,) = paid.payments
    assert (payment.cash_session_id, payment.received_by, payment.change) == (4, 7, D("20.00"))
    assert cash_register.locks == 1
    assert s.orders.locks == [order.id]
    assert s.activity.entries == [
        (
            RESTAURANT,
            WAITER.user_id,
            ActivityKind.ORDER_CHARGED,
            "Pedido #1: S/ 28.00 en Efectivo + S/ 2.00 de propina",
        )
    ]
    assert s.last_event()[4] == order.id


async def test_split_bill_logs_each_part_and_the_closing() -> None:
    s = DiningRoom()
    order = await s.served(items=(NewItem(LOMO.id), NewItem(LOMO.id)))
    charge = ChargeOrder(s.orders, _cash_register(), s.activity, s.events)
    first_item_id = order.items[0].id or 0

    await charge(
        ChargeOrderCommand(
            WAITER, order.id or 0, PaymentMethod.YAPE, amount=D("20"), expected_balance=D("56.00")
        )
    )
    await charge(
        ChargeOrderCommand(WAITER, order.id or 0, PaymentMethod.CARD, item_ids=[first_item_id])
    )
    final = await charge(ChargeOrderCommand(WAITER, order.id or 0, PaymentMethod.PLIN))

    assert final.status is OrderStatus.PAID
    assert final.payments[1].item_ids == (first_item_id,)
    assert [e[2:] for e in s.activity.entries] == [
        (ActivityKind.PAYMENT_RECEIVED, "Pedido #1: S/ 20.00 en Yape; faltan S/ 36.00"),
        (ActivityKind.PAYMENT_RECEIVED, "Pedido #1: S/ 28.00 en Tarjeta; faltan S/ 8.00"),
        (
            ActivityKind.PAYMENT_RECEIVED,
            "Pedido #1: S/ 8.00 en Plin; faltan S/ 0.00 (cuenta cerrada)",
        ),
    ]


async def test_without_open_cash_register_nothing_is_charged_or_touched() -> None:
    s = DiningRoom()
    order = await s.served()

    with pytest.raises(CashRegisterClosedError):
        await ChargeOrder(s.orders, FakeCashRegister(), s.activity, s.events)(
            ChargeOrderCommand(WAITER, order.id or 0, PaymentMethod.CASH)
        )
    assert s.orders.rows[order.id or 0].payments == []
    assert s.activity.entries == []


async def test_waiter_cannot_charge_anothers_order_but_owner_can() -> None:
    s = DiningRoom()
    order = await s.served(OTHER_WAITER)
    charge = ChargeOrder(s.orders, _cash_register(), s.activity, s.events)

    with pytest.raises(NotYourOrderError):
        await charge(ChargeOrderCommand(WAITER, order.id or 0, PaymentMethod.CASH))
    paid = await charge(ChargeOrderCommand(OWNER, order.id or 0, PaymentMethod.CASH))

    assert paid.payments[0].received_by == OWNER.user_id
    assert s.activity.entries[0][3] == "Pedido #1: S/ 28.00 en Efectivo"


# -- Mesas ----------------------------------------------------------------------------------------


async def test_creating_tables_puts_them_last_without_repeated_names() -> None:
    tables, activity = FakeTables(), RecordingActivity()
    create = CreateTable(tables, activity)

    first = await create(CreateTableCommand(RESTAURANT, 9, " Terraza   1 "))
    second = await create(CreateTableCommand(RESTAURANT, 9, "Barra"))
    with pytest.raises(TableLabelTakenError) as repeated:
        await create(CreateTableCommand(RESTAURANT, 9, "barra"))

    assert [(m.label, m.position) for m in (first, second)] == [("Terraza 1", 0), ("Barra", 1)]
    assert repeated.value.label == "barra"
    assert activity.entries == [
        (RESTAURANT, 9, ActivityKind.TABLE_CREATED, "Terraza 1"),
        (RESTAURANT, 9, ActivityKind.TABLE_CREATED, "Barra"),
    ]


async def test_editing_a_table_renames_or_deactivates_and_logs_it() -> None:
    tables, activity = FakeTables(), RecordingActivity()
    first = await tables.add(DiningTable(RESTAURANT, "1"))
    await tables.add(DiningTable(RESTAURANT, "2"))
    edit = UpdateTable(tables, activity)

    renamed = await edit(UpdateTableCommand(RESTAURANT, 9, first.id or 0, label=" Ventana "))
    inactive = await edit(UpdateTableCommand(RESTAURANT, 9, first.id or 0, is_active=False))
    same = await edit(UpdateTableCommand(RESTAURANT, 9, first.id or 0, label="VENTANA"))
    with pytest.raises(TableLabelTakenError):
        await edit(UpdateTableCommand(RESTAURANT, 9, first.id or 0, label="2"))
    with pytest.raises(TableNotFoundError) as foreign:
        await edit(UpdateTableCommand(OTHER_RESTAURANT, 9, first.id or 0, label="x"))

    assert (renamed.label, renamed.is_active) == ("Ventana", True)
    assert (inactive.label, inactive.is_active) == ("Ventana", False)
    assert same.label == "VENTANA"
    assert tables.rows[first.id or 0].label == "VENTANA"
    assert foreign.value.table_id == first.id
    assert [e[2:] for e in activity.entries] == [
        (ActivityKind.TABLE_UPDATED, "Ventana (activa)"),
        (ActivityKind.TABLE_UPDATED, "Ventana (inactiva)"),
        (ActivityKind.TABLE_UPDATED, "VENTANA (inactiva)"),
    ]


async def test_tables_show_whether_they_are_occupied_and_by_whom() -> None:
    s = DiningRoom()
    free = await s.table("1")
    occupied = await s.table("2")
    await s.table("Retirada", is_active=False)
    order = await s.order(WAITER, table_id=occupied.id)
    await s.order(OTHER_WAITER, type=OrderType.TAKEAWAY)
    staff = FakeStaff(u7="Luis")
    list_all = ListTables(s.tables, s.orders, staff)

    views = await list_all(ListTablesQuery(RESTAURANT))
    all_tables = await list_all(ListTablesQuery(RESTAURANT, include_inactive=True))

    assert [(v.table.id, v.status, v.waiter_name) for v in views] == [
        (free.id, TableStatus.FREE, ""),
        (occupied.id, TableStatus.OCCUPIED, "Luis"),
    ]
    assert views[1].active_order is order
    assert len(all_tables) == 3
    assert staff.lookups[0] == {7}


def test_table_without_order_is_free() -> None:
    assert TableView(DiningTable(RESTAURANT, "1"), None).status is TableStatus.FREE


async def test_reordering_tables_saves_the_positions() -> None:
    tables = FakeTables()
    first = await tables.add(DiningTable(RESTAURANT, "1"))
    second = await tables.add(DiningTable(RESTAURANT, "2", position=1))

    ordered = await ReorderTables(tables)(
        ReorderTablesCommand(RESTAURANT, [second.id or 0, first.id or 0])
    )

    assert [(m.id, m.position) for m in ordered] == [(second.id, 0), (first.id, 1)]
    assert tables.positions == [[second.id, first.id]]


# -- Caja -----------------------------------------------------------------------------------------


async def test_opening_the_cash_register_logs_it_and_notifies_the_whole_restaurant() -> None:
    cash_register, activity, events = FakeCashRegister(), RecordingActivity(), RecordingEvents()

    session = await OpenCash(cash_register, activity, events)(
        OpenCashCommand(OWNER, D("150"), "sencillo")
    )

    assert (session.opened_by, session.opening_amount, session.opening_notes) == (
        9,
        D("150.00"),
        "sencillo",
    )
    assert cash_register.locks == 1
    assert activity.entries == [
        (RESTAURANT, 9, ActivityKind.CASH_OPENED, "Turno 1 con S/ 150.00 de inicial")
    ]
    (event,) = events.published
    assert (event.topic, event.everyone, event.reference_id, event.user_ids) == (
        CASH_TOPIC,
        True,
        session.id,
        frozenset(),
    )
    with pytest.raises(CashRegisterAlreadyOpenError):
        await OpenCash(cash_register, activity, events)(OpenCashCommand(OWNER, D("1")))


async def test_closing_the_cash_register_signs_the_count_with_the_difference() -> None:
    s = DiningRoom()
    session = CashSession(RESTAURANT, opened_by=9, opening_amount=D("100"), id=4)
    cash_register = FakeCashRegister(session)
    cash_register.recorded_payments = [
        CashPayment(1, 1, 7, 7, PaymentMethod.CASH, D("50.00"), D("5.00"), datetime.now(UTC)),
        CashPayment(2, 2, 8, 8, PaymentMethod.YAPE, D("30.00"), D("0.00"), datetime.now(UTC)),
    ]
    await s.order()
    staff = FakeStaff(u7="Luis", u9="Rosa")
    describe = DescribeCash(cash_register, s.orders, staff)

    open_session = await describe(session)
    view = await CloseCash(cash_register, describe, s.activity, s.events)(
        CloseCashCommand(OWNER, D("150"), "faltó sencillo")
    )

    assert (open_session.open_orders, open_session.opened_by_name, open_session.closed_by_name) == (
        1,
        "Rosa",
        "",
    )
    assert open_session.summary.expected_cash == D("155.00")
    assert (view.session.counted_cash, view.session.expected_cash) == (D("150.00"), D("155.00"))
    assert view.session.difference == D("-5.00")
    assert (view.open_orders, view.closed_by_name) == (0, "Rosa")
    assert staff.lookups[-1] == {7, 8, 9}
    assert s.activity.entries[-1] == (
        RESTAURANT,
        9,
        ActivityKind.CASH_CLOSED,
        "Turno 4: contó S/ 150.00, esperado S/ 155.00, diferencia S/ -5.00",
    )
    assert s.last_event()[1:] == (CASH_TOPIC, frozenset(), True, 4)
    assert cash_register.locks == 1


async def test_cannot_close_without_an_open_cash_register() -> None:
    s = DiningRoom()
    cash_register = FakeCashRegister()

    with pytest.raises(CashSessionNotFoundError) as error:
        await CloseCash(
            cash_register, DescribeCash(cash_register, s.orders, FakeStaff()), s.activity, s.events
        )(CloseCashCommand(OWNER, D("1")))
    assert str(error.value) == "No hay una caja abierta."


async def test_reading_the_current_cash_session_a_shift_and_the_history() -> None:
    session = CashSession(RESTAURANT, opened_by=9, opening_amount=D("100"), id=4)
    cash_register = FakeCashRegister(session)

    assert await ReadCurrentCash(cash_register)(RESTAURANT) is session
    assert await ReadCurrentCash(cash_register)(OTHER_RESTAURANT) is None
    assert await ReadCashSession(cash_register)(RESTAURANT, 4) is session
    with pytest.raises(CashSessionNotFoundError) as error:
        await ReadCashSession(cash_register)(OTHER_RESTAURANT, 4)
    assert error.value.session_id == 4
    page = await ListCashSessions(cash_register)(RESTAURANT, 10, 0)
    assert (page.items, page.total) == ([session], 1)
    assert (await ListCashSessions(cash_register)(RESTAURANT, 10, 1)).items == []
    assert cash_register.locks == 0
