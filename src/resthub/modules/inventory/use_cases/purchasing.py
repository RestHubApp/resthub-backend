"""Proveedores y órdenes de compra.

Leer exige `inventory.read`; crear, enviar, recibir y cancelar,
`inventory.manage`. Recibir una orden registra cada línea como una compra en el
libro de stock, así el costo del insumo se pondera igual que al comprar a mano.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.pagination import Page
from resthub.modules.inventory.domain.exceptions import (
    IngredientInactive,
    PurchaseOrderNotFound,
    SupplierNameTaken,
    SupplierNotFound,
)
from resthub.modules.inventory.domain.purchasing import (
    COVER_DAYS,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseSuggestion,
    Receipt,
    Supplier,
    suggest_purchase,
)
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.purchasing_repository import (
    PurchaseOrderQuery,
    PurchaseOrderRepository,
    SupplierRepository,
    UsageReader,
)
from resthub.modules.inventory.use_cases.manage_ingredients import find_ingredient
from resthub.modules.inventory.use_cases.register_movements import (
    RegisterPurchase,
    RegisterPurchaseCommand,
)

# -- Proveedores ---------------------------------------------------------------


async def find_supplier(
    suppliers: SupplierRepository, restaurant_id: int, supplier_id: int
) -> Supplier:
    supplier = await suppliers.get(restaurant_id, supplier_id)
    if supplier is None:
        raise SupplierNotFound(supplier_id)
    return supplier


@dataclass(frozen=True, slots=True)
class SupplierData:
    name: str
    contact: str = ""
    phone: str = ""
    notes: str = ""
    is_active: bool = True


class SaveSupplier:
    """Da de alta un proveedor o edita uno existente (con `supplier_id`)."""

    def __init__(self, suppliers: SupplierRepository, activity: ActivityRecorder) -> None:
        self._suppliers = suppliers
        self._activity = activity

    async def __call__(
        self, restaurant_id: int, actor_id: int, data: SupplierData, supplier_id: int | None = None
    ) -> Supplier:
        candidate = Supplier(
            restaurant_id=restaurant_id,
            name=data.name,
            contact=data.contact,
            phone=data.phone,
            notes=data.notes,
            is_active=data.is_active,
        )
        same_name = await self._suppliers.find_by_name(restaurant_id, candidate.name)
        if same_name is not None and same_name.id != supplier_id:
            raise SupplierNameTaken(candidate.name)
        if supplier_id is None:
            saved = await self._suppliers.add(candidate)
            kind = ActivityKind.SUPPLIER_CREATED
        else:
            current = await find_supplier(self._suppliers, restaurant_id, supplier_id)
            candidate.id, candidate.created_at = current.id, current.created_at
            saved = await self._suppliers.save(candidate)
            kind = ActivityKind.SUPPLIER_UPDATED
        await self._activity.record(restaurant_id, actor_id, kind, saved.name)
        return saved


# -- Órdenes de compra ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineData:
    ingredient_id: int
    quantity: Decimal
    unit_cost: Decimal


async def _lines(
    ingredients: IngredientRepository, restaurant_id: int, lines: Sequence[LineData]
) -> list[PurchaseOrderLine]:
    built = []
    for line in lines:
        ingredient = await find_ingredient(ingredients, restaurant_id, line.ingredient_id)
        if not ingredient.is_active:
            raise IngredientInactive(ingredient.name)
        built.append(
            PurchaseOrderLine(
                ingredient_id=line.ingredient_id, quantity=line.quantity, unit_cost=line.unit_cost
            )
        )
    return built


class CreatePurchaseOrder:
    def __init__(
        self,
        orders: PurchaseOrderRepository,
        suppliers: SupplierRepository,
        ingredients: IngredientRepository,
        activity: ActivityRecorder,
    ) -> None:
        self._orders = orders
        self._suppliers = suppliers
        self._ingredients = ingredients
        self._activity = activity

    async def __call__(
        self,
        restaurant_id: int,
        actor_id: int,
        supplier_id: int,
        lines: Sequence[LineData],
        notes: str = "",
    ) -> PurchaseOrder:
        supplier = await find_supplier(self._suppliers, restaurant_id, supplier_id)
        created = await self._orders.add(
            PurchaseOrder(
                restaurant_id=restaurant_id,
                number=await self._orders.next_number(restaurant_id),
                supplier_id=supplier.id or 0,
                created_by=actor_id,
                lines=await _lines(self._ingredients, restaurant_id, lines),
                notes=notes,
            )
        )
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.PURCHASE_ORDER_CREATED,
            f"OC {created.number} a {supplier.name} (S/ {created.estimated_total})",
        )
        return created


async def find_purchase_order(
    orders: PurchaseOrderRepository, restaurant_id: int, order_id: int, *, for_update: bool = False
) -> PurchaseOrder:
    order = await orders.get(restaurant_id, order_id, for_update=for_update)
    if order is None:
        raise PurchaseOrderNotFound(order_id)
    return order


class EditPurchaseOrder:
    """Cambia las líneas o la nota de un borrador."""

    def __init__(self, orders: PurchaseOrderRepository, ingredients: IngredientRepository) -> None:
        self._orders = orders
        self._ingredients = ingredients

    async def __call__(
        self, restaurant_id: int, order_id: int, lines: Sequence[LineData], notes: str | None
    ) -> PurchaseOrder:
        order = await find_purchase_order(self._orders, restaurant_id, order_id, for_update=True)
        order.replace_lines(await _lines(self._ingredients, restaurant_id, lines), notes)
        return await self._orders.save(order)


class ChangePurchaseOrderStatus:
    """Enviar al proveedor o cancelar."""

    def __init__(self, orders: PurchaseOrderRepository, activity: ActivityRecorder) -> None:
        self._orders = orders
        self._activity = activity

    async def __call__(
        self, restaurant_id: int, actor_id: int, order_id: int, action: str
    ) -> PurchaseOrder:
        order = await find_purchase_order(self._orders, restaurant_id, order_id, for_update=True)
        now = datetime.now(UTC)
        if action == "send":
            order.send(now)
            kind = ActivityKind.PURCHASE_ORDER_SENT
        else:
            order.cancel(now)
            kind = ActivityKind.PURCHASE_ORDER_CANCELLED
        saved = await self._orders.save(order)
        await self._activity.record(restaurant_id, actor_id, kind, f"OC {saved.number}")
        return saved


class ReceivePurchaseOrder:
    """Llegó la mercadería: lo recibido entra al stock como compras."""

    def __init__(
        self,
        orders: PurchaseOrderRepository,
        suppliers: SupplierRepository,
        purchase: RegisterPurchase,
        activity: ActivityRecorder,
    ) -> None:
        self._orders = orders
        self._suppliers = suppliers
        self._purchase = purchase
        self._activity = activity

    async def __call__(
        self, restaurant_id: int, actor_id: int, order_id: int, receipts: Sequence[Receipt]
    ) -> PurchaseOrder:
        order = await find_purchase_order(self._orders, restaurant_id, order_id, for_update=True)
        supplier = await find_supplier(self._suppliers, restaurant_id, order.supplier_id)
        arrived = order.receive(receipts, datetime.now(UTC))
        saved = await self._orders.save(order)
        for line in arrived:
            await self._purchase(
                RegisterPurchaseCommand(
                    restaurant_id=restaurant_id,
                    actor_id=actor_id,
                    ingredient_id=line.ingredient_id,
                    quantity=line.received_quantity or Decimal(0),
                    unit_cost=line.received_unit_cost or Decimal(0),
                    reason=f"OC {saved.number} · {supplier.name}",
                )
            )
        await self._activity.record(
            restaurant_id,
            actor_id,
            ActivityKind.PURCHASE_ORDER_RECEIVED,
            f"OC {saved.number} de {supplier.name} (S/ {saved.received_total})",
        )
        return saved


class ListPurchaseOrders:
    def __init__(self, orders: PurchaseOrderRepository) -> None:
        self._orders = orders

    async def __call__(self, query: PurchaseOrderQuery) -> Page[PurchaseOrder]:
        return await self._orders.search(query)


class SuggestPurchases:
    """Qué insumos pedir y cuánto: una semana de consumo o el doble del mínimo."""

    def __init__(self, usage: UsageReader) -> None:
        self._usage = usage

    async def __call__(self, restaurant_id: int) -> list[PurchaseSuggestion]:
        since = datetime.now(UTC) - timedelta(days=COVER_DAYS)
        suggestions = []
        for row in await self._usage.usage_since(restaurant_id, since):
            suggestion = suggest_purchase(
                row.ingredient_id,
                row.stock,
                row.min_stock,
                row.used / COVER_DAYS,
                row.unit_cost,
            )
            if suggestion is not None:
                suggestions.append(suggestion)
        return suggestions
