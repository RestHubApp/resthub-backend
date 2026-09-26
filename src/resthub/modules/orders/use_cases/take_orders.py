"""Casos de uso del mesero: abrir pedidos, cargar platos, enviarlos y servirlos.

Todos exigen `orders.take`, que tienen los dos roles. Cada cambio avisa al
tablero en vivo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from resthub.core.identity import Principal
from resthub.core.local_time import local_date
from resthub.core.realtime import EventPublisher
from resthub.modules.orders.domain.exceptions import (
    CustomerNotFound,
    DishNotFound,
    DishUnavailable,
    InvalidOrder,
    TableInactive,
    TableOccupied,
)
from resthub.modules.orders.domain.modifiers import choose_modifiers
from resthub.modules.orders.domain.orders import Order, OrderItem, OrderStatus, OrderType
from resthub.modules.orders.ports.customer_directory import CustomerDirectory
from resthub.modules.orders.ports.menu_catalog import MenuCatalog
from resthub.modules.orders.ports.order_repository import OrderRepository
from resthub.modules.orders.ports.restaurant_clock import RestaurantClock
from resthub.modules.orders.ports.sent_to_kitchen_hook import (
    KitchenItem,
    SentOrder,
    SentToKitchenHook,
)
from resthub.modules.orders.ports.served_order_hook import (
    ServedOrder,
    ServedOrderHook,
    ServedPortion,
)
from resthub.modules.orders.ports.table_repository import TableRepository
from resthub.modules.orders.use_cases.manage_tables import find_table
from resthub.modules.orders.use_cases.shared import announce, find_visible_order


def notify_kitchen(hook: SentToKitchenHook, order: Order) -> None:
    hook.order_sent(
        SentOrder(
            restaurant_id=order.restaurant_id,
            order_id=order.id or 0,
            notes=order.notes,
            items=tuple(
                KitchenItem(
                    order_item_id=item.id or 0,
                    menu_item_id=item.menu_item_id,
                    name=item.name,
                    notes=item.notes,
                )
                for item in order.items
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class NewItem:
    menu_item_id: int
    quantity: int = 1
    notes: str = ""
    # Lo elegido en cada grupo de opciones: (grupo, opción).
    modifiers: tuple[tuple[str, str], ...] = ()


async def snapshot_items(
    menu: MenuCatalog, restaurant_id: int, new_items: Sequence[NewItem], now: datetime
) -> list[OrderItem]:
    """Convierte lo que pidió la mesa en ítems con nombre y precio de hoy.

    Solo entra lo que está en la carta y disponible: el mesero puede tener el
    menú desactualizado en el celular, pero el servidor no.
    """
    dishes = await menu.get_dishes(restaurant_id, {new.menu_item_id for new in new_items})
    items: list[OrderItem] = []
    for new in new_items:
        dish = dishes.get(new.menu_item_id)
        if dish is None:
            raise DishNotFound(new.menu_item_id)
        if not dish.can_be_ordered:
            raise DishUnavailable(dish.name)
        chosen = choose_modifiers(dish.name, dish.modifier_groups, new.modifiers)
        items.append(
            OrderItem(
                menu_item_id=dish.id,
                name=dish.name,
                unit_price=dish.price + sum((m.price for m in chosen), Decimal(0)),
                quantity=new.quantity,
                notes=new.notes,
                modifiers=chosen,
                created_at=now,
            )
        )
    return items


@dataclass(frozen=True, slots=True)
class OpenOrderCommand:
    actor: Principal
    type: OrderType
    table_id: int | None = None
    customer_name: str = ""
    # Delivery: teléfono, dirección y referencia. Con un cliente de la libreta
    # se completan solos si vienen vacíos.
    customer_phone: str = ""
    delivery_address: str = ""
    delivery_reference: str = ""
    customer_id: int | None = None
    client_request_id: str | None = None
    notes: str = ""
    # Puede venir vacío: el mesero abre la mesa al sentar a la gente y carga
    # los platos cuando piden.
    items: tuple[NewItem, ...] = ()


class OpenOrder:
    def __init__(
        self,
        orders: OrderRepository,
        tables: TableRepository,
        menu: MenuCatalog,
        clock: RestaurantClock,
        events: EventPublisher,
        customers: CustomerDirectory | None = None,
    ) -> None:
        self._orders = orders
        self._tables = tables
        self._menu = menu
        self._clock = clock
        self._events = events
        self._customers = customers

    async def __call__(self, command: OpenOrderCommand) -> Order:
        now = datetime.now(UTC)
        restaurant_id = command.actor.restaurant_id
        # Primero se reserva la numeración del local: a partir de acá ningún
        # otro pedido del mismo restaurante puede tomar el número siguiente ni
        # la misma mesa hasta que esta transacción termine.
        timezone = await self._clock.timezone_for_numbering(restaurant_id)
        if command.client_request_id:
            # El celular reintenta lo que ya llegó (volvió la señal, doble
            # toque): se devuelve el pedido que abrió la primera vez.
            already = await self._orders.by_client_request(restaurant_id, command.client_request_id)
            if already is not None:
                return already
        contact = await self._contact(command)

        if command.type is OrderType.DINE_IN:
            if command.table_id is None:
                raise InvalidOrder("Un pedido en mesa necesita la mesa.")
            table = await find_table(self._tables, restaurant_id, command.table_id)
            if not table.is_active:
                raise TableInactive(table.label)
            current = await self._orders.active_for_table(restaurant_id, command.table_id)
            if current is not None:
                raise TableOccupied(table.label, current.id or 0)

        items = await snapshot_items(self._menu, restaurant_id, command.items, now)
        business_date = local_date(now, timezone)
        order = Order(
            restaurant_id=restaurant_id,
            number=await self._orders.last_number(restaurant_id, business_date) + 1,
            business_date=business_date,
            type=command.type,
            waiter_id=command.actor.user_id,
            table_id=command.table_id,
            customer_name=contact[0],
            customer_phone=contact[1],
            delivery_address=contact[2],
            delivery_reference=contact[3],
            customer_id=contact[4],
            client_request_id=command.client_request_id,
            notes=command.notes,
            items=items,
            created_at=now,
            updated_at=now,
        )
        created = await self._orders.add(order)
        announce(self._events, created)
        return created

    async def _contact(self, command: OpenOrderCommand) -> tuple[str, str, str, str, int | None]:
        """Nombre, teléfono, dirección, referencia y cliente; lo vacío sale de la libreta.

        Sin cliente elegido, un teléfono que ya está en la libreta lo identifica:
        el pedido cuenta en sus visitas aunque el mesero no lo haya buscado.
        """
        given = (
            command.customer_name,
            command.customer_phone,
            command.delivery_address,
            command.delivery_reference,
        )
        if self._customers is None:
            return (*given, command.customer_id)
        restaurant_id = command.actor.restaurant_id
        if command.customer_id is None:
            match = await self._customers.by_phone(restaurant_id, command.customer_phone)
            return (*given, None if match is None else match.id)
        known = await self._customers.get(restaurant_id, command.customer_id)
        if known is None:
            raise CustomerNotFound(command.customer_id)
        return (
            given[0] or known.name,
            given[1] or known.phone,
            given[2] or known.address,
            given[3] or known.reference,
            known.id,
        )


@dataclass(frozen=True, slots=True)
class AddItemsCommand:
    actor: Principal
    order_id: int
    items: tuple[NewItem, ...]


class AddItems:
    """Suma platos a un pedido activo.

    Si ya estaba listo o servido vuelve a cocina: hay algo nuevo que preparar.
    Si el pedido ya estaba en cocina, los platos nuevos van directo a preparar,
    así que se avisa igual que al enviarlo.
    """

    def __init__(
        self,
        orders: OrderRepository,
        menu: MenuCatalog,
        events: EventPublisher,
        kitchen_hook: SentToKitchenHook,
    ) -> None:
        self._orders = orders
        self._menu = menu
        self._events = events
        self._kitchen_hook = kitchen_hook

    async def __call__(self, command: AddItemsCommand) -> Order:
        now = datetime.now(UTC)
        order = await find_visible_order(
            self._orders, command.actor, command.order_id, for_update=True
        )
        # El estado se revisa antes de mirar el menú: pedir platos para un
        # pedido cobrado es un error del pedido, no del plato.
        order.ensure_accepts_items()
        items = await snapshot_items(self._menu, command.actor.restaurant_id, command.items, now)
        order.add_items(items, now)
        saved = await self._orders.save(order)
        announce(self._events, saved)
        if saved.status is OrderStatus.IN_KITCHEN:
            notify_kitchen(self._kitchen_hook, saved)
        return saved


@dataclass(frozen=True, slots=True)
class ChangeItemCommand:
    actor: Principal
    order_id: int
    item_id: int
    quantity: int | None = None
    notes: str | None = None


class ChangeItem:
    """Corrige cantidad o nota de un plato mientras el pedido sigue abierto."""

    def __init__(self, orders: OrderRepository, events: EventPublisher) -> None:
        self._orders = orders
        self._events = events

    async def __call__(self, command: ChangeItemCommand) -> Order:
        order = await find_visible_order(
            self._orders, command.actor, command.order_id, for_update=True
        )
        order.change_item(
            command.item_id, datetime.now(UTC), quantity=command.quantity, notes=command.notes
        )
        saved = await self._orders.save(order)
        announce(self._events, saved)
        return saved


@dataclass(frozen=True, slots=True)
class RemoveItemCommand:
    actor: Principal
    order_id: int
    item_id: int


class RemoveItem:
    def __init__(self, orders: OrderRepository, events: EventPublisher) -> None:
        self._orders = orders
        self._events = events

    async def __call__(self, command: RemoveItemCommand) -> Order:
        order = await find_visible_order(
            self._orders, command.actor, command.order_id, for_update=True
        )
        order.remove_item(command.item_id, datetime.now(UTC))
        saved = await self._orders.save(order)
        announce(self._events, saved)
        return saved


@dataclass(frozen=True, slots=True)
class UpdateOrderDetailsCommand:
    actor: Principal
    order_id: int
    notes: str | None = None
    customer_name: str | None = None
    # Delivery: teléfono, dirección y referencia; `None` en uno lo deja igual.
    delivery: tuple[str | None, str | None, str | None] | None = None


class UpdateOrderDetails:
    """Nota general del pedido o nombre del cliente, mientras siga activo."""

    def __init__(self, orders: OrderRepository, events: EventPublisher) -> None:
        self._orders = orders
        self._events = events

    async def __call__(self, command: UpdateOrderDetailsCommand) -> Order:
        order = await find_visible_order(
            self._orders, command.actor, command.order_id, for_update=True
        )
        order.update_details(
            datetime.now(UTC),
            notes=command.notes,
            customer_name=command.customer_name,
            delivery=command.delivery,
        )
        saved = await self._orders.save(order)
        announce(self._events, saved)
        return saved


class SendToKitchen:
    def __init__(
        self, orders: OrderRepository, events: EventPublisher, kitchen_hook: SentToKitchenHook
    ) -> None:
        self._orders = orders
        self._events = events
        self._kitchen_hook = kitchen_hook

    async def __call__(self, actor: Principal, order_id: int) -> Order:
        order = await find_visible_order(self._orders, actor, order_id, for_update=True)
        order.send_to_kitchen(datetime.now(UTC))
        saved = await self._orders.save(order)
        announce(self._events, saved)
        notify_kitchen(self._kitchen_hook, saved)
        return saved


class MarkServed:
    """El mesero llevó los platos a la mesa (o entregó el pedido para llevar).

    Es el momento en que los insumos se gastan de verdad, así que acá se avisa
    al puerto de pedidos servidos, que en la aplicación descuenta el
    inventario según las recetas.
    """

    def __init__(
        self, orders: OrderRepository, served_hook: ServedOrderHook, events: EventPublisher
    ) -> None:
        self._orders = orders
        self._served_hook = served_hook
        self._events = events

    async def __call__(self, actor: Principal, order_id: int) -> Order:
        order = await find_visible_order(self._orders, actor, order_id, for_update=True)
        order.mark_served(datetime.now(UTC))
        saved = await self._orders.save(order)
        await self._served_hook.order_served(
            ServedOrder(
                restaurant_id=saved.restaurant_id,
                order_id=saved.id or 0,
                actor_id=actor.user_id,
                portions=tuple(
                    ServedPortion(
                        order_item_id=item.id or 0,
                        menu_item_id=item.menu_item_id,
                        quantity=item.quantity,
                    )
                    for item in saved.items
                ),
            )
        )
        announce(self._events, saved)
        return saved
