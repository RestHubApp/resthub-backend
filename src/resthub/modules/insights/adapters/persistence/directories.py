"""Adaptadores de lectores hacia tablas ajenas.

Los indicadores no tienen datos propios salvo las decisiones de la IA: leen
pedidos (`orders`), platos (`menu`), insumos, recetas y movimientos
(`inventory`), personal (`accounts`) y la zona del restaurante
(`restaurants`). Se lee, nunca se escribe. Las tablas se describen con
`table()` y `column()` sueltos, no con los modelos ORM de sus dueños:
importarlos rompería la independencia entre módulos, y así igual se conservan
los tipos (montos como `Decimal`, fechas como `datetime`).

Toda fecha que se compara contra la base se pasa en UTC. SQLite guarda las
fechas sin zona, como texto, y compararlas contra una hora de Lima correría el
rango cinco horas.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    and_,
    case,
    column,
    func,
    select,
    table,
)
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.core.timestamps import as_utc
from resthub.modules.insights.domain.decisions import KitchenNote, SubjectType
from resthub.modules.insights.domain.period import DateRange
from resthub.modules.insights.domain.sales import CANCELLED, PAID, CatalogDish, OrderFact, SoldDish
from resthub.modules.insights.domain.stock import IngredientFlow, StockFact, WasteFact
from resthub.modules.insights.ports.stock_directory import FlowWindows
from resthub.modules.insights.ports.subject_directory import SubjectDescription, SubjectKey

_restaurants = table("restaurants", column("id", Integer), column("timezone", String))
_users = table(
    "users", column("id", Integer), column("restaurant_id", Integer), column("full_name", String)
)
_orders = table(
    "orders",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("business_date", Date),
    column("number", Integer),
    column("status", String),
    column("notes", String),
    column("total", Numeric(10, 2)),
    column("payment_method", String),
    column("waiter_id", Integer),
    column("created_at", DateTime(timezone=True)),
)
_order_items = table(
    "order_items",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("order_id", Integer),
    column("menu_item_id", Integer),
    column("name", String),
    column("unit_price", Numeric(10, 2)),
    column("quantity", Integer),
    column("notes", String),
)
_menu_items = table(
    "menu_items",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("category_id", Integer),
    column("name", String),
    column("price", Numeric(10, 2)),
    column("is_active", Boolean),
    column("position", Integer),
)
_menu_categories = table(
    "menu_categories", column("id", Integer), column("name", String), column("position", Integer)
)
_ingredients = table(
    "ingredients",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("name", String),
    column("unit", String),
    column("min_stock", Numeric(12, 3)),
    column("unit_cost", Numeric(14, 6)),
    column("is_active", Boolean),
)
_recipe_lines = table(
    "recipe_lines",
    column("restaurant_id", Integer),
    column("menu_item_id", Integer),
    column("ingredient_id", Integer),
    column("quantity", Numeric(12, 3)),
)
_movements = table(
    "stock_movements",
    column("id", Integer),
    column("restaurant_id", Integer),
    column("ingredient_id", Integer),
    column("kind", String),
    column("quantity", Numeric(12, 3)),
    column("unit_cost", Numeric(14, 6)),
    column("reason", String),
    column("created_at", DateTime(timezone=True)),
)

# Los estados de `orders` que este módulo necesita nombrar.
_ACTIVE_STATUSES = ("open", "in_kitchen", "ready", "served")
_CONSUMPTION = "consumption"
_WASTE = "waste"
_PURCHASE = "purchase"


def _decimal(value: Any) -> Decimal:
    """Una suma de la base, como `Decimal`.

    SQLite suma en coma flotante: se pasa por texto para no arrastrar
    decimales espurios (0.1 + 0.2 tiene que seguir siendo 0.3 al redondear).
    """
    if value is None:
        return Decimal(0)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


class SqlRestaurantCalendar:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def timezone(self, restaurant_id: int) -> str:
        result = await self._session.execute(
            select(_restaurants.c.timezone).where(_restaurants.c.id == restaurant_id)
        )
        timezone = result.scalar_one_or_none()
        return str(timezone) if timezone else DEFAULT_TIMEZONE


class SqlSalesDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def closed_orders(self, restaurant_id: int, period: DateRange) -> list[OrderFact]:
        result = await self._session.execute(
            select(
                _orders.c.id,
                _orders.c.business_date,
                _orders.c.created_at,
                _orders.c.status,
                _orders.c.total,
                _orders.c.payment_method,
                _orders.c.waiter_id,
            ).where(
                _orders.c.restaurant_id == restaurant_id,
                _orders.c.status.in_((PAID, CANCELLED)),
                _orders.c.business_date.between(period.start, period.end),
            )
        )
        return [
            OrderFact(
                id=int(row.id),
                business_date=row.business_date,
                created_at=as_utc(row.created_at),
                status=str(row.status),
                total=_decimal(row.total),
                payment_method=row.payment_method,
                waiter_id=int(row.waiter_id),
            )
            for row in result
        ]

    async def sold_dishes(self, restaurant_id: int, period: DateRange) -> list[SoldDish]:
        result = await self._session.execute(
            select(
                _order_items.c.menu_item_id,
                func.max(_order_items.c.name).label("name"),
                func.sum(_order_items.c.quantity).label("quantity"),
                func.sum(_order_items.c.unit_price * _order_items.c.quantity).label("revenue"),
            )
            .join(_orders, _orders.c.id == _order_items.c.order_id)
            .where(
                _order_items.c.restaurant_id == restaurant_id,
                _orders.c.restaurant_id == restaurant_id,
                _orders.c.status == PAID,
                _orders.c.business_date.between(period.start, period.end),
            )
            .group_by(_order_items.c.menu_item_id)
        )
        return [
            SoldDish(
                menu_item_id=int(row.menu_item_id),
                name=str(row.name),
                quantity=int(row.quantity or 0),
                revenue=_decimal(row.revenue),
            )
            for row in result
        ]

    async def dish_catalog(self, restaurant_id: int) -> list[CatalogDish]:
        lines = await self._session.execute(
            select(_recipe_lines.c.menu_item_id, _recipe_lines.c.quantity, _ingredients.c.unit_cost)
            .join(_ingredients, _ingredients.c.id == _recipe_lines.c.ingredient_id)
            .where(_recipe_lines.c.restaurant_id == restaurant_id)
        )
        # El costo se suma en Python y no en SQL: cantidad por costo con seis
        # decimales tiene que ser exacto en SQLite también.
        costs: dict[int, Decimal] = defaultdict(Decimal)
        for line in lines:
            costs[int(line.menu_item_id)] += _decimal(line.quantity) * _decimal(line.unit_cost)

        dishes = await self._session.execute(
            select(
                _menu_items.c.id,
                _menu_items.c.name,
                _menu_items.c.price,
                _menu_items.c.is_active,
                _menu_categories.c.name.label("category"),
            )
            .join(_menu_categories, _menu_categories.c.id == _menu_items.c.category_id)
            .where(_menu_items.c.restaurant_id == restaurant_id)
            .order_by(_menu_categories.c.position, _menu_items.c.position, _menu_items.c.id)
        )
        return [
            CatalogDish(
                menu_item_id=int(row.id),
                name=str(row.name),
                category=str(row.category),
                price=_decimal(row.price),
                is_active=bool(row.is_active),
                recipe_cost=costs.get(int(row.id)),
            )
            for row in dishes
        ]

    async def staff_names(self, restaurant_id: int, user_ids: Collection[int]) -> dict[int, str]:
        if not user_ids:
            return {}
        result = await self._session.execute(
            select(_users.c.id, _users.c.full_name).where(
                _users.c.restaurant_id == restaurant_id, _users.c.id.in_(list(user_ids))
            )
        )
        return {int(row.id): str(row.full_name) for row in result}


class SqlStockDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stock_levels(self, restaurant_id: int) -> list[StockFact]:
        totals = (
            select(
                _movements.c.ingredient_id,
                func.sum(_movements.c.quantity).label("stock"),
            )
            .where(_movements.c.restaurant_id == restaurant_id)
            .group_by(_movements.c.ingredient_id)
            .subquery()
        )
        result = await self._session.execute(
            select(
                _ingredients.c.id,
                _ingredients.c.name,
                _ingredients.c.unit,
                _ingredients.c.min_stock,
                totals.c.stock,
            )
            .outerjoin(totals, totals.c.ingredient_id == _ingredients.c.id)
            .where(_ingredients.c.restaurant_id == restaurant_id, _ingredients.c.is_active)
            .order_by(_ingredients.c.name)
        )
        return [
            StockFact(
                ingredient_id=int(row.id),
                name=str(row.name),
                unit=str(row.unit),
                stock=_decimal(row.stock),
                min_stock=_decimal(row.min_stock),
            )
            for row in result
        ]

    async def flows(self, restaurant_id: int, windows: FlowWindows) -> dict[int, IngredientFlow]:
        short_since, long_since = _utc(windows.short_since), _utc(windows.long_since)
        kind, moment, amount = _movements.c.kind, _movements.c.created_at, _movements.c.quantity

        def outflow(movement_kind: str, since: datetime) -> Any:
            # Las salidas se guardan restando; acá se suman en positivo.
            return func.sum(case((and_(kind == movement_kind, moment >= since), -amount), else_=0))

        result = await self._session.execute(
            select(
                _movements.c.ingredient_id,
                outflow(_CONSUMPTION, short_since).label("consumed_short"),
                outflow(_CONSUMPTION, long_since).label("consumed_long"),
                outflow(_WASTE, long_since).label("wasted_long"),
                func.min(moment).label("first_movement_at"),
                func.max(case((kind == _PURCHASE, moment)), type_=DateTime(timezone=True)).label(
                    "last_purchase_at"
                ),
            )
            .where(_movements.c.restaurant_id == restaurant_id)
            .group_by(_movements.c.ingredient_id)
        )
        return {
            int(row.ingredient_id): IngredientFlow(
                consumed_short=_decimal(row.consumed_short),
                consumed_long=_decimal(row.consumed_long),
                wasted_long=_decimal(row.wasted_long),
                first_movement_at=as_utc(row.first_movement_at) if row.first_movement_at else None,
                last_purchase_at=as_utc(row.last_purchase_at) if row.last_purchase_at else None,
            )
            for row in result
        }

    async def wastes(
        self,
        restaurant_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[WasteFact]:
        statement = (
            select(
                _movements.c.id,
                _movements.c.ingredient_id,
                _movements.c.quantity,
                _movements.c.unit_cost,
                _movements.c.reason,
                _movements.c.created_at,
                _ingredients.c.name,
                _ingredients.c.unit,
            )
            .join(_ingredients, _ingredients.c.id == _movements.c.ingredient_id)
            .where(_movements.c.restaurant_id == restaurant_id, _movements.c.kind == _WASTE)
            .order_by(_movements.c.created_at.desc(), _movements.c.id.desc())
        )
        if since is not None:
            statement = statement.where(_movements.c.created_at >= _utc(since))
        if until is not None:
            statement = statement.where(_movements.c.created_at < _utc(until))
        if limit is not None:
            statement = statement.limit(limit)
        result = await self._session.execute(statement)
        facts: list[WasteFact] = []
        for row in result:
            lost = -_decimal(row.quantity)
            facts.append(
                WasteFact(
                    movement_id=int(row.id),
                    ingredient_id=int(row.ingredient_id),
                    ingredient_name=str(row.name),
                    unit=str(row.unit),
                    quantity=lost,
                    cost=lost * _decimal(row.unit_cost),
                    reason=str(row.reason),
                    created_at=as_utc(row.created_at),
                )
            )
        return facts


class SqlKitchenNotesDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_notes(self, restaurant_id: int) -> list[KitchenNote]:
        return await self._notes(restaurant_id, _orders.c.status.in_(_ACTIVE_STATUSES))

    async def notes_for_orders(
        self, restaurant_id: int, order_ids: Collection[int]
    ) -> list[KitchenNote]:
        if not order_ids:
            return []
        return await self._notes(restaurant_id, _orders.c.id.in_(list(order_ids)))

    async def _notes(self, restaurant_id: int, condition: Any) -> list[KitchenNote]:
        orders = await self._session.execute(
            select(_orders.c.id, _orders.c.notes)
            .where(_orders.c.restaurant_id == restaurant_id, condition)
            .order_by(_orders.c.id)
        )
        notes: list[KitchenNote] = []
        order_ids: list[int] = []
        for row in orders:
            order_ids.append(int(row.id))
            if (row.notes or "").strip():
                notes.append(
                    KitchenNote(
                        subject_type=SubjectType.ORDER,
                        subject_id=int(row.id),
                        order_id=int(row.id),
                        text=str(row.notes).strip(),
                    )
                )
        if not order_ids:
            return notes
        items = await self._session.execute(
            select(
                _order_items.c.id,
                _order_items.c.order_id,
                _order_items.c.name,
                _order_items.c.notes,
            )
            .where(
                _order_items.c.restaurant_id == restaurant_id,
                _order_items.c.order_id.in_(order_ids),
                _order_items.c.notes != "",
            )
            .order_by(_order_items.c.order_id, _order_items.c.id)
        )
        notes.extend(
            KitchenNote(
                subject_type=SubjectType.ORDER_ITEM,
                subject_id=int(row.id),
                order_id=int(row.order_id),
                text=str(row.notes).strip(),
                dish_name=str(row.name),
            )
            for row in items
        )
        return notes


class SqlSubjectDirectory:
    """Una consulta por tipo de asunto presente en la página, no una por decisión."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def describe(
        self, restaurant_id: int, subjects: Collection[SubjectKey]
    ) -> dict[SubjectKey, SubjectDescription]:
        ids: dict[SubjectType, list[int]] = defaultdict(list)
        for subject_type, subject_id in subjects:
            ids[subject_type].append(subject_id)
        described: dict[SubjectKey, SubjectDescription] = {}
        if ids[SubjectType.INGREDIENT]:
            result = await self._session.execute(
                select(_ingredients.c.id, _ingredients.c.name).where(
                    _ingredients.c.restaurant_id == restaurant_id,
                    _ingredients.c.id.in_(ids[SubjectType.INGREDIENT]),
                )
            )
            for row in result:
                described[(SubjectType.INGREDIENT, int(row.id))] = SubjectDescription(
                    label=str(row.name)
                )
        if ids[SubjectType.ORDER]:
            result = await self._session.execute(
                select(_orders.c.id, _orders.c.number).where(
                    _orders.c.restaurant_id == restaurant_id,
                    _orders.c.id.in_(ids[SubjectType.ORDER]),
                )
            )
            for row in result:
                described[(SubjectType.ORDER, int(row.id))] = SubjectDescription(
                    order_number=int(row.number)
                )
        if ids[SubjectType.ORDER_ITEM]:
            result = await self._session.execute(
                select(_order_items.c.id, _order_items.c.name, _orders.c.number)
                .join(_orders, _orders.c.id == _order_items.c.order_id)
                .where(
                    _order_items.c.restaurant_id == restaurant_id,
                    _order_items.c.id.in_(ids[SubjectType.ORDER_ITEM]),
                )
            )
            for row in result:
                described[(SubjectType.ORDER_ITEM, int(row.id))] = SubjectDescription(
                    order_number=int(row.number), label=str(row.name)
                )
        if ids[SubjectType.STOCK_MOVEMENT]:
            result = await self._session.execute(
                select(_movements.c.id, _ingredients.c.name)
                .join(_ingredients, _ingredients.c.id == _movements.c.ingredient_id)
                .where(
                    _movements.c.restaurant_id == restaurant_id,
                    _movements.c.id.in_(ids[SubjectType.STOCK_MOVEMENT]),
                )
            )
            for row in result:
                described[(SubjectType.STOCK_MOVEMENT, int(row.id))] = SubjectDescription(
                    label=str(row.name)
                )
        return described
