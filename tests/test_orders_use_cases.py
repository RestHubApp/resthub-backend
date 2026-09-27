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
    CashRegisterAlreadyOpen,
    CashRegisterClosed,
    CashSessionNotFound,
    CustomerNotFound,
    DiscountNotAllowed,
    DishNotFound,
    DishUnavailable,
    InvalidOrder,
    InvalidTransition,
    NotYourOrder,
    OrderNotFound,
    TableInactive,
    TableLabelTaken,
    TableNotFound,
    TableOccupied,
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

LOCAL = 1
OTRO = 2
D = Decimal
TOMAR = frozenset({Permission.ORDERS_TAKE.value, Permission.ORDERS_CHARGE.value})
TODO = frozenset(p.value for p in Permission)
MESERO = Principal(user_id=7, role_id=2, is_active=True, restaurant_id=LOCAL, permissions=TOMAR)
OTRO_MESERO = replace(MESERO, user_id=8)
ENCARGADO = Principal(user_id=9, role_id=1, is_active=True, restaurant_id=LOCAL, permissions=TODO)


# -- Dobles --------------------------------------------------------------------------------


class Pedidos:
    def __init__(self) -> None:
        self.rows: dict[int, Order] = {}
        self.bloqueos: list[int] = []
        self.consultas: list[OrderQuery] = []
        self.uniones: list[tuple[int, int]] = []

    async def add(self, order: Order) -> Order:
        order.id = len(self.rows) + 1
        for i, item in enumerate(order.items):
            item.id = 100 * order.id + i
        self.rows[order.id] = order
        return order

    async def get(self, restaurant_id: int, order_id: int, *, for_update: bool = False):
        if for_update:
            self.bloqueos.append(order_id)
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
        self.uniones.append((target.id or 0, source.id or 0))
        self.rows[source.id or 0] = source
        return await self.save(target)

    async def search(self, query: OrderQuery) -> Page[Order]:
        self.consultas.append(query)
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


class Mesas:
    def __init__(self) -> None:
        self.rows: dict[int, DiningTable] = {}
        self.posiciones: list[list[int]] = []

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
        self.posiciones.append([t.id or 0 for t in tables])
        for t in tables:
            self.rows[t.id or 0] = replace(t)


class Carta:
    def __init__(self, *platos: OrderableDish) -> None:
        self.platos = {p.id: p for p in platos}

    async def get_dishes(self, restaurant_id: int, dish_ids: Collection[int]):
        return {i: p for i, p in self.platos.items() if i in dish_ids}


class Reloj:
    def __init__(self) -> None:
        self.llamadas = 0

    async def timezone_for_numbering(self, restaurant_id: int) -> str:
        self.llamadas += 1
        return "America/Lima"


class Personal:
    def __init__(self, **nombres: str) -> None:
        self.nombres = {int(k.removeprefix("u")): v for k, v in nombres.items()}
        self.pedidos: list[set[int]] = []

    async def names(self, restaurant_id: int, user_ids: Collection[int]) -> dict[int, str]:
        self.pedidos.append(set(user_ids))
        return {i: n for i, n in self.nombres.items() if i in user_ids}


class Libreta:
    def __init__(self, *clientes: KnownCustomer) -> None:
        self.clientes = list(clientes)

    async def get(self, restaurant_id: int, customer_id: int) -> KnownCustomer | None:
        return next((c for c in self.clientes if c.id == customer_id), None)

    async def by_phone(self, restaurant_id: int, phone: str) -> KnownCustomer | None:
        return next((c for c in self.clientes if phone and c.phone == phone), None)


class Cocina:
    def __init__(self) -> None:
        self.enviados: list[SentOrder] = []
        self.servidos: list[ServedOrder] = []

    def order_sent(self, sent: SentOrder) -> None:
        self.enviados.append(sent)

    async def order_served(self, served: ServedOrder) -> None:
        self.servidos.append(served)


class Caja:
    def __init__(self, abierta: CashSession | None = None) -> None:
        self.actual = abierta
        self.turnos: dict[int, CashSession] = {}
        self.bloqueos = 0
        self.pagos: list[CashPayment] = []
        self.ajustes: list[CashOrderAdjustment] = []
        if abierta is not None:
            abierta.id = abierta.id or 1
            self.turnos[abierta.id] = abierta

    async def current(self, restaurant_id: int, *, for_update: bool = False):
        if for_update:
            self.bloqueos += 1
        if self.actual and self.actual.restaurant_id == restaurant_id and self.actual.is_open:
            return self.actual
        return None

    async def get(self, restaurant_id: int, session_id: int) -> CashSession | None:
        found = self.turnos.get(session_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def open(self, session: CashSession) -> CashSession:
        session.id = len(self.turnos) + 1
        self.turnos[session.id] = session
        self.actual = session
        return session

    async def save(self, session: CashSession) -> CashSession:
        self.turnos[session.id or 0] = session
        return session

    async def history(self, restaurant_id: int, limit: int, offset: int) -> Page[CashSession]:
        items = [t for t in self.turnos.values() if t.restaurant_id == restaurant_id]
        return Page(items=items[offset : offset + limit], total=len(items))

    async def payments(self, restaurant_id: int, session_id: int) -> list[CashPayment]:
        return list(self.pagos)

    async def adjustments(self, restaurant_id: int, session_id: int) -> list[CashOrderAdjustment]:
        return list(self.ajustes)


class Tope:
    def __init__(self, limite: str = "10") -> None:
        self.limite = D(limite)
        self.consultas = 0

    async def waiter_limit(self, restaurant_id: int) -> Decimal:
        self.consultas += 1
        return self.limite


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
AGOTADO = OrderableDish(3, "Ceviche", D("30"), True, False)
SIN_STOCK = OrderableDish(4, "Causa", D("15"), True, True, out_of_stock=True)
RETIRADO = OrderableDish(5, "Seco", D("25"), False, True)


class Salon:
    def __init__(self) -> None:
        self.pedidos = Pedidos()
        self.mesas = Mesas()
        self.carta = Carta(LOMO, PIZZA, AGOTADO, SIN_STOCK, RETIRADO)
        self.reloj = Reloj()
        self.avisos = RecordingEvents()
        self.bitacora = RecordingActivity()
        self.cocina = Cocina()

    async def mesa(self, label: str = "1", **extra: object) -> DiningTable:
        return await self.mesas.add(DiningTable(LOCAL, label, **extra))  # type: ignore[arg-type]

    def abrir(self, clientes: Libreta | None = None) -> OpenOrder:
        return OpenOrder(self.pedidos, self.mesas, self.carta, self.reloj, self.avisos, clientes)

    async def pedido(
        self, actor: Principal = MESERO, table_id: int | None = None, **extra: object
    ) -> Order:
        if table_id is None and "type" not in extra:
            table_id = (await self.mesa(f"M{len(self.mesas.rows) + 1}")).id
        comando = OpenOrderCommand(
            actor=actor,
            type=extra.pop("type", OrderType.DINE_IN),  # type: ignore[arg-type]
            table_id=table_id,
            items=extra.pop("items", (NewItem(LOMO.id),)),  # type: ignore[arg-type]
            **extra,  # type: ignore[arg-type]
        )
        return await self.abrir()(comando)

    async def servido(self, actor: Principal = MESERO, **extra: object) -> Order:
        pedido = await self.pedido(actor, **extra)
        await SendToKitchen(self.pedidos, self.avisos, self.cocina)(actor, pedido.id or 0)
        await MarkReady(self.pedidos, self.avisos)(ENCARGADO, pedido.id or 0)
        await MarkServed(self.pedidos, self.cocina, self.avisos)(actor, pedido.id or 0)
        self.pedidos.bloqueos.clear()
        self.avisos.published.clear()
        return pedido

    def aviso(self) -> tuple[int, str, frozenset[int], bool, int | None]:
        e = self.avisos.published[-1]
        return (e.restaurant_id, e.topic, e.user_ids, e.everyone, e.reference_id)


# -- Abrir pedidos ------------------------------------------------------------------------


async def test_abrir_un_pedido_en_mesa_congela_platos_y_avisa_al_local() -> None:
    s = Salon()
    mesa = await s.mesa()

    pedido = await s.abrir()(
        OpenOrderCommand(
            MESERO,
            OrderType.DINE_IN,
            table_id=mesa.id,
            customer_name="Ana",
            notes="sin ají",
            items=(
                NewItem(LOMO.id, quantity=2, notes="bien cocido"),
                NewItem(PIZZA.id, modifiers=(("tamaño", "familiar"),)),
            ),
        )
    )

    assert (pedido.number, pedido.waiter_id, pedido.table_id, pedido.status) == (
        1,
        MESERO.user_id,
        mesa.id,
        OrderStatus.OPEN,
    )
    assert (pedido.customer_name, pedido.notes) == ("Ana", "sin ají")
    assert [(i.name, i.quantity, i.unit_price, i.notes) for i in pedido.items] == [
        ("Lomo saltado", 2, D("28.00"), "bien cocido"),
        ("Pizza", 1, D("42.50"), ""),
    ]
    assert [(m.group, m.option) for m in pedido.items[1].modifiers] == [("Tamaño", "Familiar")]
    assert pedido.total == D("98.50")
    assert s.reloj.llamadas == 1
    assert s.aviso() == (LOCAL, ORDERS_TOPIC, frozenset({7}), True, pedido.id)


async def test_la_numeracion_sigue_el_dia_del_local() -> None:
    s = Salon()

    primero = await s.pedido()
    segundo = await s.pedido(type=OrderType.TAKEAWAY)

    assert (primero.number, segundo.number) == (1, 2)
    assert segundo.table_id is None


async def test_un_reintento_con_el_mismo_identificador_devuelve_el_mismo_pedido() -> None:
    s = Salon()
    mesa = await s.mesa()
    comando = OpenOrderCommand(
        MESERO, OrderType.DINE_IN, table_id=mesa.id, client_request_id="abc-1"
    )

    primero = await s.abrir()(comando)
    segundo = await s.abrir()(comando)

    assert segundo is primero
    assert len(s.pedidos.rows) == 1
    assert len(s.avisos.published) == 1


@pytest.mark.parametrize(
    ("items", "error", "mensaje"),
    [
        ((NewItem(99),), DishNotFound, "No existe el plato 99."),
        ((NewItem(AGOTADO.id),), DishUnavailable, "Ceviche no está disponible hoy."),
        ((NewItem(SIN_STOCK.id),), DishUnavailable, "Causa no está disponible hoy."),
        ((NewItem(RETIRADO.id),), DishUnavailable, "Seco no está disponible hoy."),
        ((NewItem(PIZZA.id),), InvalidOrder, "Elige tamaño para Pizza."),
    ],
)
async def test_solo_entra_lo_que_esta_en_la_carta_y_se_puede_pedir(
    items: tuple[NewItem, ...], error: type[Exception], mensaje: str
) -> None:
    s = Salon()

    with pytest.raises(error) as fallo:
        await s.pedido(items=items)
    assert str(fallo.value) == mensaje
    assert s.pedidos.rows == {}


async def test_una_mesa_inactiva_u_ocupada_no_recibe_otro_pedido() -> None:
    s = Salon()
    inactiva = await s.mesa("Terraza", is_active=False)
    ocupada = await s.mesa("2")
    primero = await s.pedido(table_id=ocupada.id)

    with pytest.raises(TableInactive) as inactiva_err:
        await s.pedido(table_id=inactiva.id)
    with pytest.raises(TableOccupied) as ocupada_err:
        await s.pedido(table_id=ocupada.id)
    with pytest.raises(TableNotFound):
        await s.pedido(table_id=999)
    with pytest.raises(InvalidOrder) as sin_mesa:
        await s.abrir()(OpenOrderCommand(MESERO, OrderType.DINE_IN))

    assert str(inactiva_err.value) == "La mesa Terraza está desactivada."
    assert (ocupada_err.value.label, ocupada_err.value.order_id) == ("2", primero.id)
    assert str(sin_mesa.value) == "Un pedido en mesa necesita la mesa."
    assert len(s.pedidos.rows) == 1


async def test_un_cliente_de_la_libreta_completa_lo_que_falta() -> None:
    s = Salon()
    ana = KnownCustomer(5, "Ana", "987654321", "Jr. Pizarro 450", "portón verde")

    pedido = await s.abrir(Libreta(ana))(
        OpenOrderCommand(MESERO, OrderType.DELIVERY, customer_id=5, delivery_reference="timbre 2")
    )

    assert (
        pedido.customer_name,
        pedido.customer_phone,
        pedido.delivery_address,
        pedido.delivery_reference,
        pedido.customer_id,
    ) == ("Ana", "987654321", "Jr. Pizarro 450", "timbre 2", 5)


async def test_lo_escrito_por_el_mesero_manda_sobre_la_libreta() -> None:
    s = Salon()
    ana = KnownCustomer(5, "Ana", "987654321", "Jr. Pizarro 450", "portón verde")

    pedido = await s.abrir(Libreta(ana))(
        OpenOrderCommand(
            MESERO,
            OrderType.DELIVERY,
            customer_id=5,
            customer_name="Ana T.",
            customer_phone="911",
            delivery_address="Av. Grau 1",
        )
    )

    assert (pedido.customer_name, pedido.customer_phone, pedido.delivery_address) == (
        "Ana T.",
        "911",
        "Av. Grau 1",
    )
    assert pedido.delivery_reference == "portón verde"


async def test_un_telefono_conocido_identifica_al_cliente_y_uno_ajeno_no() -> None:
    s = Salon()
    ana = KnownCustomer(5, "Ana", "987654321", "Jr. Pizarro 450", "")
    abrir = s.abrir(Libreta(ana))

    conocido = await abrir(
        OpenOrderCommand(
            MESERO,
            OrderType.DELIVERY,
            customer_name="A",
            customer_phone="987654321",
            delivery_address="x",
        )
    )
    nuevo = await abrir(
        OpenOrderCommand(
            MESERO,
            OrderType.DELIVERY,
            customer_name="B",
            customer_phone="911",
            delivery_address="y",
        )
    )
    with pytest.raises(CustomerNotFound) as ajeno:
        await abrir(OpenOrderCommand(MESERO, OrderType.TAKEAWAY, customer_id=77))

    assert (conocido.customer_id, conocido.customer_name) == (5, "A")
    assert nuevo.customer_id is None
    assert ajeno.value.customer_id == 77


async def test_sin_libreta_el_cliente_queda_como_vino() -> None:
    s = Salon()

    pedido = await s.abrir()(OpenOrderCommand(MESERO, OrderType.TAKEAWAY, customer_id=3))

    assert pedido.customer_id == 3


# -- Platos y estados ---------------------------------------------------------------------------


async def test_agregar_platos_en_cocina_los_manda_directo_a_preparar() -> None:
    s = Salon()
    pedido = await s.pedido()
    await SendToKitchen(s.pedidos, s.avisos, s.cocina)(MESERO, pedido.id or 0)
    s.cocina.enviados.clear()

    guardado = await AddItems(s.pedidos, s.carta, s.avisos, s.cocina)(
        AddItemsCommand(MESERO, pedido.id or 0, (NewItem(LOMO.id, notes="para llevar"),))
    )

    assert guardado.status is OrderStatus.IN_KITCHEN
    assert len(guardado.items) == 2
    (enviado,) = s.cocina.enviados
    assert (enviado.restaurant_id, enviado.order_id, len(enviado.items)) == (
        LOCAL,
        pedido.id,
        2,
    )
    assert enviado.items[1].notes == "para llevar"
    assert s.pedidos.bloqueos[-1] == pedido.id
    assert s.aviso()[4] == pedido.id


async def test_agregar_a_un_pedido_abierto_no_avisa_a_cocina() -> None:
    s = Salon()
    pedido = await s.pedido()

    await AddItems(s.pedidos, s.carta, s.avisos, s.cocina)(
        AddItemsCommand(MESERO, pedido.id or 0, (NewItem(LOMO.id),))
    )

    assert s.cocina.enviados == []
    assert len(s.avisos.published) == 2


async def test_a_un_pedido_cerrado_se_le_rechaza_antes_de_mirar_la_carta() -> None:
    s = Salon()
    pedido = await s.pedido()
    await CancelOrder(s.pedidos, s.bitacora, s.avisos)(
        CancelOrderCommand(MESERO, pedido.id or 0, "Se fueron")
    )

    with pytest.raises(InvalidTransition):
        await AddItems(s.pedidos, s.carta, s.avisos, s.cocina)(
            AddItemsCommand(MESERO, pedido.id or 0, (NewItem(99),))
        )


async def test_cambiar_y_quitar_platos_guardan_y_avisan() -> None:
    s = Salon()
    pedido = await s.pedido(items=(NewItem(LOMO.id), NewItem(LOMO.id)))
    uno, dos = (i.id or 0 for i in pedido.items)

    cambiado = await ChangeItem(s.pedidos, s.avisos)(
        ChangeItemCommand(MESERO, pedido.id or 0, uno, quantity=3, notes="sin sal")
    )
    quitado = await RemoveItem(s.pedidos, s.avisos)(RemoveItemCommand(MESERO, pedido.id or 0, dos))

    assert [(i.id, i.quantity, i.notes) for i in cambiado.items] == [(uno, 3, "sin sal")]
    assert quitado.item_count == 3
    assert s.pedidos.bloqueos[-2:] == [pedido.id, pedido.id]
    assert len(s.avisos.published) == 3


async def test_editar_los_datos_del_pedido_llega_al_dominio() -> None:
    s = Salon()
    pedido = await s.pedido(
        type=OrderType.DELIVERY,
        customer_name="Ana",
        customer_phone="987",
        delivery_address="Jr. Pizarro 450",
    )

    guardado = await UpdateOrderDetails(s.pedidos, s.avisos)(
        UpdateOrderDetailsCommand(
            MESERO,
            pedido.id or 0,
            notes="tocar timbre",
            customer_name="Ana T.",
            delivery=("911", None, "portón"),
        )
    )

    assert (guardado.notes, guardado.customer_name, guardado.customer_phone) == (
        "tocar timbre",
        "Ana T.",
        "911",
    )
    assert (guardado.delivery_address, guardado.delivery_reference) == ("Jr. Pizarro 450", "portón")
    assert s.pedidos.bloqueos[-1] == pedido.id
    assert s.aviso()[4] == pedido.id


async def test_servir_avisa_al_inventario_con_cada_porcion() -> None:
    s = Salon()
    pedido = await s.pedido(
        items=(NewItem(LOMO.id, quantity=2), NewItem(PIZZA.id, modifiers=(("Tamaño", "Personal"),)))
    )
    await SendToKitchen(s.pedidos, s.avisos, s.cocina)(MESERO, pedido.id or 0)
    await MarkReady(s.pedidos, s.avisos)(ENCARGADO, pedido.id or 0)

    servido = await MarkServed(s.pedidos, s.cocina, s.avisos)(MESERO, pedido.id or 0)

    assert servido.status is OrderStatus.SERVED
    (aviso,) = s.cocina.servidos
    assert (aviso.restaurant_id, aviso.order_id, aviso.actor_id) == (LOCAL, pedido.id, 7)
    assert [(p.order_item_id, p.menu_item_id, p.quantity) for p in aviso.portions] == [
        (pedido.items[0].id, LOMO.id, 2),
        (pedido.items[1].id, PIZZA.id, 1),
    ]
    (enviado,) = s.cocina.enviados
    assert [(k.menu_item_id, k.name) for k in enviado.items] == [
        (LOMO.id, "Lomo saltado"),
        (PIZZA.id, "Pizza"),
    ]
    assert s.pedidos.bloqueos == [pedido.id, pedido.id, pedido.id]


async def test_cancelar_anota_el_total_y_el_motivo() -> None:
    s = Salon()
    pedido = await s.pedido()

    cancelado = await CancelOrder(s.pedidos, s.bitacora, s.avisos)(
        CancelOrderCommand(ENCARGADO, pedido.id or 0, "  Se  fueron ")
    )

    assert cancelado.status is OrderStatus.CANCELLED
    assert s.bitacora.entries == [
        (LOCAL, ENCARGADO.user_id, ActivityKind.ORDER_CANCELLED, "Pedido #1 (S/ 28.00): Se fueron")
    ]
    assert s.aviso()[4] == pedido.id
    assert s.pedidos.bloqueos == [pedido.id]


# -- Lo que ve cada quien ---------------------------------------------------------------------


async def test_el_mesero_no_ve_lo_cerrado_de_otro_pero_si_lo_activo() -> None:
    s = Salon()
    activo = await s.pedido(OTRO_MESERO)
    cerrado = await s.pedido(OTRO_MESERO)
    await CancelOrder(s.pedidos, s.bitacora, s.avisos)(
        CancelOrderCommand(ENCARGADO, cerrado.id or 0, "x")
    )
    leer = ReadOrder(s.pedidos)

    assert (await leer(MESERO, activo.id or 0)).id == activo.id
    assert (await leer(ENCARGADO, cerrado.id or 0)).id == cerrado.id
    assert (await leer(OTRO_MESERO, cerrado.id or 0)).id == cerrado.id
    with pytest.raises(OrderNotFound) as oculto:
        await leer(MESERO, cerrado.id or 0)
    with pytest.raises(OrderNotFound):
        await leer(replace(ENCARGADO, restaurant_id=OTRO), activo.id or 0)
    assert oculto.value.order_id == cerrado.id


async def test_el_historial_del_mesero_se_acota_a_lo_suyo() -> None:
    s = Salon()
    listar = ListOrders(s.pedidos)
    filtros = {
        "statuses": frozenset({OrderStatus.PAID}),
        "date_from": date(2026, 9, 1),
        "date_to": date(2026, 9, 30),
        "type": OrderType.TAKEAWAY,
        "table_id": 3,
        "waiter_id": 8,
        "limit": 5,
        "offset": 10,
    }

    await listar(ListOrdersQuery(MESERO, **filtros))  # type: ignore[arg-type]
    await listar(ListOrdersQuery(ENCARGADO))

    assert s.pedidos.consultas == [
        OrderQuery(restaurant_id=LOCAL, visible_to=MESERO.user_id, **filtros),  # type: ignore[arg-type]
        OrderQuery(restaurant_id=LOCAL, visible_to=None),
    ]


async def test_el_tablero_muestra_todos_los_activos() -> None:
    s = Salon()
    mio = await s.pedido(MESERO)
    ajeno = await s.pedido(OTRO_MESERO)

    activos = await ListActiveOrders(s.pedidos)(MESERO)

    assert [o.id for o in activos] == [mio.id, ajeno.id]


async def test_describir_pedidos_suma_mesa_y_nombres_en_una_consulta() -> None:
    s = Salon()
    mesa = await s.mesa("Terraza 2")
    en_mesa = await s.pedido(MESERO, table_id=mesa.id)
    llevar = await s.pedido(OTRO_MESERO, type=OrderType.TAKEAWAY)
    en_mesa.discounted_by = 9
    personal = Personal(u7="Luis", u9="Rosa")

    vistas = await DescribeOrders(s.mesas, personal)(LOCAL, [en_mesa, llevar])

    assert [(v.table_label, v.waiter_name) for v in vistas] == [("Terraza 2", "Luis"), (None, "")]
    assert personal.pedidos == [{7, 8, 9}]
    assert vistas[0].staff_names == {7: "Luis", 9: "Rosa"}
    assert await DescribeOrders(s.mesas, personal)(LOCAL, []) == []
    assert (await DescribeOrders(s.mesas, personal).one(LOCAL, llevar)).order is llevar


# -- Mover y unir ------------------------------------------------------------------------------


async def test_mover_un_pedido_a_otra_mesa_lo_anota_de_donde_a_donde() -> None:
    s = Salon()
    uno = await s.mesa("1")
    dos = await s.mesa("Barra")
    pedido = await s.pedido(table_id=uno.id)
    mover = MoveOrder(s.pedidos, s.mesas, s.reloj, s.bitacora, s.avisos)

    movido = await mover(MoveOrderCommand(MESERO, pedido.id or 0, dos.id or 0))

    assert movido.table_id == dos.id
    assert s.bitacora.entries == [
        (LOCAL, MESERO.user_id, ActivityKind.ORDER_MOVED, "Pedido #1: de 1 a Barra")
    ]
    assert s.pedidos.bloqueos == [pedido.id]
    assert s.reloj.llamadas == 2
    assert s.aviso()[4] == pedido.id


async def test_no_se_mueve_a_una_mesa_ocupada_inactiva_o_ajena() -> None:
    s = Salon()
    uno = await s.mesa("1")
    dos = await s.mesa("2")
    retirada = await s.mesa("Terraza", is_active=False)
    pedido = await s.pedido(table_id=uno.id)
    otro = await s.pedido(table_id=dos.id)
    mover = MoveOrder(s.pedidos, s.mesas, s.reloj, s.bitacora, s.avisos)

    with pytest.raises(TableOccupied) as ocupada:
        await mover(MoveOrderCommand(MESERO, pedido.id or 0, dos.id or 0))
    with pytest.raises(TableInactive):
        await mover(MoveOrderCommand(MESERO, pedido.id or 0, retirada.id or 0))
    with pytest.raises(TableNotFound):
        await mover(MoveOrderCommand(MESERO, pedido.id or 0, 999))
    with pytest.raises(InvalidOrder):
        await mover(MoveOrderCommand(MESERO, pedido.id or 0, uno.id or 0))

    assert ocupada.value.order_id == otro.id
    assert s.pedidos.rows[pedido.id or 0].table_id == uno.id
    assert s.bitacora.entries == []


async def test_mover_desde_una_mesa_que_ya_no_existe_la_anota_con_signo() -> None:
    s = Salon()
    uno = await s.mesa("1")
    dos = await s.mesa("2")
    pedido = await s.pedido(table_id=uno.id)
    del s.mesas.rows[uno.id or 0]

    await MoveOrder(s.pedidos, s.mesas, s.reloj, s.bitacora, s.avisos)(
        MoveOrderCommand(MESERO, pedido.id or 0, dos.id or 0)
    )

    assert s.bitacora.entries[-1][3] == "Pedido #1: de ? a 2"


async def test_unir_mesas_toma_ambas_en_orden_y_avisa_por_las_dos() -> None:
    s = Salon()
    queda = await s.pedido()
    se_une = await s.pedido(items=(NewItem(LOMO.id), NewItem(LOMO.id)))
    unir = MergeOrders(s.pedidos, s.bitacora, s.avisos)

    unido = await unir(MergeOrdersCommand(MESERO, se_une.id or 0, queda.id or 0))

    assert unido.id == se_une.id
    assert len(unido.items) == 3
    assert s.pedidos.rows[queda.id or 0].status is OrderStatus.CANCELLED
    assert s.pedidos.bloqueos == [queda.id, se_une.id]
    assert s.pedidos.uniones == [(se_une.id, queda.id)]
    assert s.bitacora.entries == [
        (LOCAL, MESERO.user_id, ActivityKind.ORDER_MERGED, "Pedido #1 unido al #2 (S/ 84.00)")
    ]
    assert [e.reference_id for e in s.avisos.published[-2:]] == [queda.id, se_une.id]


async def test_un_mesero_solo_une_mesas_suyas() -> None:
    s = Salon()
    mia = await s.pedido(MESERO)
    ajena = await s.pedido(OTRO_MESERO)

    with pytest.raises(NotYourOrder) as error:
        await MergeOrders(s.pedidos, s.bitacora, s.avisos)(
            MergeOrdersCommand(MESERO, mia.id or 0, ajena.id or 0)
        )
    assert str(error.value) == (
        "Solo el mesero que tomó el pedido o el encargado pueden unir esas mesas."
    )
    unido = await MergeOrders(s.pedidos, s.bitacora, s.avisos)(
        MergeOrdersCommand(ENCARGADO, mia.id or 0, ajena.id or 0)
    )
    assert unido.id == mia.id


# -- Descuentos y cortesías ----------------------------------------------------------------------


async def test_el_mesero_descuenta_hasta_su_tope_y_queda_anotado() -> None:
    s = Salon()
    pedido = await s.servido()
    tope = Tope("10")
    descontar = ApplyDiscount(s.pedidos, tope, s.bitacora, s.avisos)

    with pytest.raises(DiscountNotAllowed):
        await descontar(ApplyDiscountCommand(MESERO, pedido.id or 0, D("15"), "amigo"))
    guardado = await descontar(ApplyDiscountCommand(MESERO, pedido.id or 0, D("10"), "frecuente"))
    quitado = await descontar(ApplyDiscountCommand(MESERO, pedido.id or 0, D("0")))

    assert tope.consultas == 3
    assert guardado.discounted_by is None and quitado.discount_percent == 0
    assert [e[3] for e in s.bitacora.entries] == [
        "Pedido #1: 10.00 % (S/ 2.80), frecuente",
        "Pedido #1: quitó el descuento",
    ]
    assert s.bitacora.entries[0][:3] == (LOCAL, MESERO.user_id, ActivityKind.ORDER_DISCOUNTED)
    assert s.pedidos.bloqueos == [pedido.id] * 3


async def test_el_encargado_descuenta_sin_tope_y_el_mesero_no_toca_pedidos_ajenos() -> None:
    s = Salon()
    pedido = await s.servido(OTRO_MESERO)
    tope = Tope("5")

    guardado = await ApplyDiscount(s.pedidos, tope, s.bitacora, s.avisos)(
        ApplyDiscountCommand(ENCARGADO, pedido.id or 0, D("50"), "cumpleaños")
    )
    with pytest.raises(NotYourOrder) as ajeno:
        await ApplyDiscount(s.pedidos, tope, s.bitacora, s.avisos)(
            ApplyDiscountCommand(MESERO, pedido.id or 0, D("1"), "x")
        )

    assert (guardado.discount_percent, guardado.discounted_by) == (D("50.00"), ENCARGADO.user_id)
    assert tope.consultas == 0
    assert ajeno.value.action == "darle un descuento"
    assert s.aviso()[4] == pedido.id


async def test_invitar_y_dejar_de_invitar_un_plato_queda_en_la_bitacora() -> None:
    s = Salon()
    pedido = await s.pedido(items=(NewItem(LOMO.id, quantity=2),))
    item = pedido.items[0].id or 0
    cortesia = SetCourtesy(s.pedidos, s.bitacora, s.avisos)

    invitado = await cortesia(CourtesyCommand(ENCARGADO, pedido.id or 0, item, "Error de cocina"))
    assert invitado.total == D("0.00")
    cobrado = await cortesia(CourtesyCommand(ENCARGADO, pedido.id or 0, item, None))

    assert cobrado.total == D("56.00")
    assert [e[2:] for e in s.bitacora.entries] == [
        (
            ActivityKind.ORDER_COURTESY,
            "Pedido #1: invitó 2 × Lomo saltado (S/ 56.00), Error de cocina",
        ),
        (ActivityKind.ORDER_COURTESY, "Pedido #1: Lomo saltado vuelve a cobrarse"),
    ]
    assert s.bitacora.entries[0][:2] == (LOCAL, ENCARGADO.user_id)
    assert s.pedidos.bloqueos[-2:] == [pedido.id, pedido.id]
    assert s.aviso()[4] == pedido.id


# -- Cobrar ---------------------------------------------------------------------------------------


def _caja() -> Caja:
    return Caja(CashSession(LOCAL, opened_by=9, opening_amount=D("100"), id=4))


async def test_cobrar_todo_de_una_vez_cae_en_la_caja_y_se_anota() -> None:
    s = Salon()
    pedido = await s.servido()
    caja = _caja()

    pagado = await ChargeOrder(s.pedidos, caja, s.bitacora, s.avisos)(
        ChargeOrderCommand(
            MESERO, pedido.id or 0, PaymentMethod.CASH, amount_received=D("50"), tip=D("2")
        )
    )

    assert pagado.status is OrderStatus.PAID
    (pago,) = pagado.payments
    assert (pago.cash_session_id, pago.received_by, pago.change) == (4, 7, D("20.00"))
    assert caja.bloqueos == 1
    assert s.pedidos.bloqueos == [pedido.id]
    assert s.bitacora.entries == [
        (
            LOCAL,
            MESERO.user_id,
            ActivityKind.ORDER_CHARGED,
            "Pedido #1: S/ 28.00 en Efectivo + S/ 2.00 de propina",
        )
    ]
    assert s.aviso()[4] == pedido.id


async def test_una_cuenta_dividida_anota_cada_parte_y_el_cierre() -> None:
    s = Salon()
    pedido = await s.servido(items=(NewItem(LOMO.id), NewItem(LOMO.id)))
    cobrar = ChargeOrder(s.pedidos, _caja(), s.bitacora, s.avisos)
    primer_plato = pedido.items[0].id or 0

    await cobrar(
        ChargeOrderCommand(
            MESERO, pedido.id or 0, PaymentMethod.YAPE, amount=D("20"), expected_balance=D("56.00")
        )
    )
    await cobrar(
        ChargeOrderCommand(MESERO, pedido.id or 0, PaymentMethod.CARD, item_ids=[primer_plato])
    )
    final = await cobrar(ChargeOrderCommand(MESERO, pedido.id or 0, PaymentMethod.PLIN))

    assert final.status is OrderStatus.PAID
    assert final.payments[1].item_ids == (primer_plato,)
    assert [e[2:] for e in s.bitacora.entries] == [
        (ActivityKind.PAYMENT_RECEIVED, "Pedido #1: S/ 20.00 en Yape; faltan S/ 36.00"),
        (ActivityKind.PAYMENT_RECEIVED, "Pedido #1: S/ 28.00 en Tarjeta; faltan S/ 8.00"),
        (
            ActivityKind.PAYMENT_RECEIVED,
            "Pedido #1: S/ 8.00 en Plin; faltan S/ 0.00 (cuenta cerrada)",
        ),
    ]


async def test_sin_caja_abierta_no_se_cobra_ni_se_toca_el_pedido() -> None:
    s = Salon()
    pedido = await s.servido()

    with pytest.raises(CashRegisterClosed):
        await ChargeOrder(s.pedidos, Caja(), s.bitacora, s.avisos)(
            ChargeOrderCommand(MESERO, pedido.id or 0, PaymentMethod.CASH)
        )
    assert s.pedidos.rows[pedido.id or 0].payments == []
    assert s.bitacora.entries == []


async def test_el_mesero_no_cobra_lo_de_otro_y_el_encargado_si() -> None:
    s = Salon()
    pedido = await s.servido(OTRO_MESERO)
    cobrar = ChargeOrder(s.pedidos, _caja(), s.bitacora, s.avisos)

    with pytest.raises(NotYourOrder):
        await cobrar(ChargeOrderCommand(MESERO, pedido.id or 0, PaymentMethod.CASH))
    pagado = await cobrar(ChargeOrderCommand(ENCARGADO, pedido.id or 0, PaymentMethod.CASH))

    assert pagado.payments[0].received_by == ENCARGADO.user_id
    assert s.bitacora.entries[0][3] == "Pedido #1: S/ 28.00 en Efectivo"


# -- Mesas ----------------------------------------------------------------------------------------


async def test_crear_mesas_las_pone_al_final_y_sin_nombres_repetidos() -> None:
    mesas, bitacora = Mesas(), RecordingActivity()
    crear = CreateTable(mesas, bitacora)

    uno = await crear(CreateTableCommand(LOCAL, 9, " Terraza   1 "))
    dos = await crear(CreateTableCommand(LOCAL, 9, "Barra"))
    with pytest.raises(TableLabelTaken) as repetida:
        await crear(CreateTableCommand(LOCAL, 9, "barra"))

    assert [(m.label, m.position) for m in (uno, dos)] == [("Terraza 1", 0), ("Barra", 1)]
    assert repetida.value.label == "barra"
    assert bitacora.entries == [
        (LOCAL, 9, ActivityKind.TABLE_CREATED, "Terraza 1"),
        (LOCAL, 9, ActivityKind.TABLE_CREATED, "Barra"),
    ]


async def test_editar_una_mesa_renombra_o_desactiva_y_lo_anota() -> None:
    mesas, bitacora = Mesas(), RecordingActivity()
    uno = await mesas.add(DiningTable(LOCAL, "1"))
    await mesas.add(DiningTable(LOCAL, "2"))
    editar = UpdateTable(mesas, bitacora)

    renombrada = await editar(UpdateTableCommand(LOCAL, 9, uno.id or 0, label=" Ventana "))
    retirada = await editar(UpdateTableCommand(LOCAL, 9, uno.id or 0, is_active=False))
    misma = await editar(UpdateTableCommand(LOCAL, 9, uno.id or 0, label="VENTANA"))
    with pytest.raises(TableLabelTaken):
        await editar(UpdateTableCommand(LOCAL, 9, uno.id or 0, label="2"))
    with pytest.raises(TableNotFound) as ajena:
        await editar(UpdateTableCommand(OTRO, 9, uno.id or 0, label="x"))

    assert (renombrada.label, renombrada.is_active) == ("Ventana", True)
    assert (retirada.label, retirada.is_active) == ("Ventana", False)
    assert misma.label == "VENTANA"
    assert mesas.rows[uno.id or 0].label == "VENTANA"
    assert ajena.value.table_id == uno.id
    assert [e[2:] for e in bitacora.entries] == [
        (ActivityKind.TABLE_UPDATED, "Ventana (activa)"),
        (ActivityKind.TABLE_UPDATED, "Ventana (inactiva)"),
        (ActivityKind.TABLE_UPDATED, "VENTANA (inactiva)"),
    ]


async def test_las_mesas_muestran_si_estan_ocupadas_y_por_quien() -> None:
    s = Salon()
    libre = await s.mesa("1")
    ocupada = await s.mesa("2")
    await s.mesa("Retirada", is_active=False)
    pedido = await s.pedido(MESERO, table_id=ocupada.id)
    await s.pedido(OTRO_MESERO, type=OrderType.TAKEAWAY)
    personal = Personal(u7="Luis")
    listar = ListTables(s.mesas, s.pedidos, personal)

    vistas = await listar(ListTablesQuery(LOCAL))
    todas = await listar(ListTablesQuery(LOCAL, include_inactive=True))

    assert [(v.table.id, v.status, v.waiter_name) for v in vistas] == [
        (libre.id, TableStatus.FREE, ""),
        (ocupada.id, TableStatus.OCCUPIED, "Luis"),
    ]
    assert vistas[1].active_order is pedido
    assert len(todas) == 3
    assert personal.pedidos[0] == {7}


def test_una_mesa_sin_pedido_esta_libre() -> None:
    assert TableView(DiningTable(LOCAL, "1"), None).status is TableStatus.FREE


async def test_reordenar_mesas_guarda_las_posiciones() -> None:
    mesas = Mesas()
    uno = await mesas.add(DiningTable(LOCAL, "1"))
    dos = await mesas.add(DiningTable(LOCAL, "2", position=1))

    ordenadas = await ReorderTables(mesas)(ReorderTablesCommand(LOCAL, [dos.id or 0, uno.id or 0]))

    assert [(m.id, m.position) for m in ordenadas] == [(dos.id, 0), (uno.id, 1)]
    assert mesas.posiciones == [[dos.id, uno.id]]


# -- Caja -----------------------------------------------------------------------------------------


async def test_abrir_la_caja_la_anota_y_avisa_a_todo_el_local() -> None:
    caja, bitacora, avisos = Caja(), RecordingActivity(), RecordingEvents()

    turno = await OpenCash(caja, bitacora, avisos)(OpenCashCommand(ENCARGADO, D("150"), "sencillo"))

    assert (turno.opened_by, turno.opening_amount, turno.opening_notes) == (
        9,
        D("150.00"),
        "sencillo",
    )
    assert caja.bloqueos == 1
    assert bitacora.entries == [
        (LOCAL, 9, ActivityKind.CASH_OPENED, "Turno 1 con S/ 150.00 de inicial")
    ]
    (aviso,) = avisos.published
    assert (aviso.topic, aviso.everyone, aviso.reference_id, aviso.user_ids) == (
        CASH_TOPIC,
        True,
        turno.id,
        frozenset(),
    )
    with pytest.raises(CashRegisterAlreadyOpen):
        await OpenCash(caja, bitacora, avisos)(OpenCashCommand(ENCARGADO, D("1")))


async def test_cerrar_la_caja_firma_el_arqueo_con_la_diferencia() -> None:
    s = Salon()
    turno = CashSession(LOCAL, opened_by=9, opening_amount=D("100"), id=4)
    caja = Caja(turno)
    caja.pagos = [
        CashPayment(1, 1, 7, 7, PaymentMethod.CASH, D("50.00"), D("5.00"), datetime.now(UTC)),
        CashPayment(2, 2, 8, 8, PaymentMethod.YAPE, D("30.00"), D("0.00"), datetime.now(UTC)),
    ]
    await s.pedido()
    personal = Personal(u7="Luis", u9="Rosa")
    describir = DescribeCash(caja, s.pedidos, personal)

    abierta = await describir(turno)
    vista = await CloseCash(caja, describir, s.bitacora, s.avisos)(
        CloseCashCommand(ENCARGADO, D("150"), "faltó sencillo")
    )

    assert (abierta.open_orders, abierta.opened_by_name, abierta.closed_by_name) == (1, "Rosa", "")
    assert abierta.summary.expected_cash == D("155.00")
    assert (vista.session.counted_cash, vista.session.expected_cash) == (D("150.00"), D("155.00"))
    assert vista.session.difference == D("-5.00")
    assert (vista.open_orders, vista.closed_by_name) == (0, "Rosa")
    assert personal.pedidos[-1] == {7, 8, 9}
    assert s.bitacora.entries[-1] == (
        LOCAL,
        9,
        ActivityKind.CASH_CLOSED,
        "Turno 4: contó S/ 150.00, esperado S/ 155.00, diferencia S/ -5.00",
    )
    assert s.aviso()[1:] == (CASH_TOPIC, frozenset(), True, 4)
    assert caja.bloqueos == 1


async def test_cerrar_sin_caja_abierta_no_se_puede() -> None:
    s = Salon()
    caja = Caja()

    with pytest.raises(CashSessionNotFound) as error:
        await CloseCash(caja, DescribeCash(caja, s.pedidos, Personal()), s.bitacora, s.avisos)(
            CloseCashCommand(ENCARGADO, D("1"))
        )
    assert str(error.value) == "No hay una caja abierta."


async def test_leer_la_caja_actual_un_turno_y_el_historial() -> None:
    turno = CashSession(LOCAL, opened_by=9, opening_amount=D("100"), id=4)
    caja = Caja(turno)

    assert await ReadCurrentCash(caja)(LOCAL) is turno
    assert await ReadCurrentCash(caja)(OTRO) is None
    assert await ReadCashSession(caja)(LOCAL, 4) is turno
    with pytest.raises(CashSessionNotFound) as error:
        await ReadCashSession(caja)(OTRO, 4)
    assert error.value.session_id == 4
    pagina = await ListCashSessions(caja)(LOCAL, 10, 0)
    assert (pagina.items, pagina.total) == ([turno], 1)
    assert (await ListCashSessions(caja)(LOCAL, 10, 1)).items == []
    assert caja.bloqueos == 0
