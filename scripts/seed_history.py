"""Siembra sesenta días de historia SINTÉTICA en el restaurante de prueba.

Solo para desarrollo y demostraciones. Nada de lo que escribe pasó de verdad:
son pedidos, consumos, compras y mermas inventados con una semilla fija, para
que el panel de indicadores tenga gráficos que mirar y la reposición tenga un
caso de cada acción. Las compras llevan "(histórico sintético)" en el motivo y
los dos meseros que crea llevan "(sintético)" en el nombre.

Qué genera, del día 60 hasta ayer (y tres pedidos en cocina hoy):

- Pedidos pagados con más movimiento viernes, sábado y domingo, en almuerzo
  (12 a 15 h) y en cena (19 a 22 h), con los feriados de Fiestas Patrias y de
  Santa Rosa más llenos y una leve subida a lo largo de los dos meses.
- Medios de pago con Yape como el más frecuente; algunos pedidos cancelados;
  notas en algunos platos, unas pocas con alergias.
- Consumo de insumos según las recetas, como si cada pedido se hubiera servido.
- Compras periódicas en el mercado (los frescos cada dos días, lo seco una vez
  por semana) calculadas para que al final del período haya de todo: insumos
  que hay que comprar hoy, otros esta semana, la mayoría que puede esperar, y
  dos con tanta merma que hay que revisarlos.
- Mermas con motivos en texto libre variados, para clasificar sus causas.

Necesita que `seed_dev.py` haya corrido antes: usa su restaurante, su carta,
sus mesas, sus insumos y sus recetas. Además mueve el "Stock inicial" de
`seed_dev` al primer día de la historia, para que el libro se lea en orden.

Es idempotente: si ya sembró una vez (existen sus meseros sintéticos), no hace
nada. Igual que `seed_dev`, se niega a correr fuera de `DEBUG` o contra una
base que no sea local.

Uso:
    uv run python scripts/seed_dev.py
    uv run python scripts/seed_history.py
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.config import get_settings
from resthub.core.database import SessionFactory, engine
from resthub.core.identity import Role
from resthub.core.local_time import local_date
from resthub.core.security import BcryptPasswordHasher
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.entities import User
from resthub.modules.inventory.adapters.persistence.models import IngredientRow, StockMovementRow
from resthub.modules.inventory.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyIngredientRepository,
    SqlAlchemyRecipeRepository,
)
from resthub.modules.inventory.domain.entities import Ingredient
from resthub.modules.menu.adapters.persistence.sqlalchemy_menu_repository import (
    SqlAlchemyMenuRepository,
)
from resthub.modules.orders.adapters.persistence.models import OrderItemRow, OrderRow
from resthub.modules.orders.adapters.persistence.sqlalchemy_table_repository import (
    SqlAlchemyTableRepository,
)
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)

DEMO_SLUG = "restaurante-demo"
ADMIN_EMAIL = "admin@resthub.dev"
WAITER_EMAIL = "mesero@resthub.dev"
DEMO_PASSWORD = "resthub123"
LOCAL_HOSTS = {None, "localhost", "127.0.0.1", "::1"}

DAYS = 60
# La misma semilla da la misma historia: dos demos se ven igual.
SEED = 20260925
SYNTHETIC = "(histórico sintético)"
PURCHASE_REASON = f"Compra en el mercado {SYNTHETIC}"
CENT = Decimal("0.01")
MILLI = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class SyntheticWaiter:
    email: str
    full_name: str


# Su existencia es la marca de que la historia ya se sembró.
SYNTHETIC_WAITERS = (
    SyntheticWaiter("carla.demo@resthub.dev", "Carla Mendoza (sintético)"),
    SyntheticWaiter("jorge.demo@resthub.dev", "Jorge Huamán (sintético)"),
)

# -- Demanda -----------------------------------------------------------------

BASE_ORDERS_PER_DAY = 28
# Lunes flojo, fin de semana lleno.
WEEKDAY_FACTOR = (0.72, 0.8, 0.86, 0.95, 1.2, 1.5, 1.38)
# Feriados de Perú que caen en cualquier ventana de sesenta días del año.
HOLIDAYS = {(7, 28): 1.6, (7, 29): 1.55, (8, 6): 1.2, (8, 30): 1.45, (10, 8): 1.3}
# Sube un 12 % de punta a punta: la tendencia se ve en el gráfico diario.
GROWTH = 0.12

# (hora local, peso). Almuerzo y cena concentran casi todo.
LUNCH_HOURS = ((12, 30), (13, 42), (14, 22), (15, 6))
DINNER_HOURS = ((19, 26), (20, 38), (21, 26), (22, 10))
OFF_HOURS = ((11, 30), (16, 25), (17, 25), (18, 20))
SERVICES = (("lunch", 56), ("dinner", 36), ("off", 8))

PARTY_SIZES = ((1, 24), (2, 34), (3, 20), (4, 16), (5, 6))

# Peso de cada plato de fondo según el servicio. El menú del día solo sale en
# el almuerzo de lunes a viernes; el ceviche es de almuerzo, como en Lima.
MAINS: dict[str, dict[str, int]] = {
    "weekday_lunch": {
        "Menú del día": 42,
        "Lomo saltado": 12,
        "Arroz chaufa de pollo": 11,
        "Ceviche clásico": 10,
        "Ají de gallina": 9,
        "Pollo saltado": 7,
        "Chicharrón de pescado": 5,
        "Chicharrón de cerdo": 4,
    },
    "weekend_lunch": {
        "Ceviche clásico": 22,
        "Lomo saltado": 16,
        "Chicharrón de pescado": 10,
        "Arroz chaufa de pollo": 10,
        "Chicharrón de cerdo": 10,
        "Ají de gallina": 8,
        "Pollo saltado": 7,
    },
    "dinner": {
        "Lomo saltado": 22,
        "Arroz chaufa de pollo": 15,
        "Pollo saltado": 12,
        "Chicharrón de cerdo": 9,
        "Ají de gallina": 9,
        "Ceviche clásico": 5,
        "Chicharrón de pescado": 5,
    },
}
STARTERS = {
    "Papa a la huancaína": 30,
    "Causa limeña": 26,
    "Leche de tigre": 22,
    "Choclo con queso": 14,
}
DRINKS = {
    "Chicha morada (vaso)": 30,
    "Inca Kola 500 ml": 18,
    "Chicha morada (jarra 1 L)": 10,
    "Limonada (jarra 1 L)": 8,
    "Coca-Cola 500 ml": 12,
    "Agua mineral": 9,
    "Cerveza Cusqueña 620 ml": 10,
    "Emoliente": 4,
}
# Las jarras rinden para tres.
PITCHERS = {"Chicha morada (jarra 1 L)", "Limonada (jarra 1 L)"}

PAYMENT_METHODS = (("yape", 40), ("cash", 27), ("card", 14), ("plin", 13), ("transfer", 6))
CANCEL_RATE = 0.035
TAKEAWAY_RATE = 0.18
ITEM_NOTE_RATE = 0.12
ORDER_NOTE_RATE = 0.04

ITEM_NOTES = (
    ("sin cebolla", 14),
    ("poco picante", 10),
    ("sin ají", 7),
    ("bien cocido", 6),
    ("arroz aparte", 5),
    ("extra limón", 5),
    ("sin culantro", 4),
    ("término medio", 3),
    ("sin hielo", 5),
    ("con poca azúcar", 4),
    ("alérgico al maní", 3),
    ("sin mariscos, es alérgica", 2),
    ("celíaca: nada con gluten ni sillao", 2),
    ("vegetariano, sin carne", 2),
    ("intolerante a la lactosa", 1),
    ("urgente, el cliente está apurado", 3),
    ("sacar primero", 2),
    ("para compartir", 3),
)
ORDER_NOTES = (
    ("Cumpleaños: traer el postre con vela", 3),
    ("Mesa con un niño alérgico al maní", 2),
    ("Cliente frecuente, atender rápido", 2),
    ("Pagan por separado", 3),
)
CUSTOMERS = ("Carmen", "Pedro", "Lucía", "Sr. Huamán", "Milagros", "Kevin", "Rosa", "Diego")
CANCEL_REASONS = (
    "El cliente se retiró antes de que salga",
    "Se demoró demasiado",
    "Pedido duplicado por error",
    "El cliente cambió de opinión",
)

# -- Almacén -----------------------------------------------------------------

# Cada cuántos días se compra cada insumo. Lo que no está acá, una vez por semana.
FRESH = {
    "Pechuga de pollo",
    "Lomo de res",
    "Pescado fresco",
    "Panceta de cerdo",
    "Tomate",
    "Limón",
    "Culantro",
    "Cebolla china",
    "Queso fresco",
    "Ají limo",
    "Choclo desgranado",
}
EVERY_FEW_DAYS = {
    "Papa amarilla",
    "Papa blanca",
    "Camote",
    "Cebolla roja",
    "Huevo",
    "Pan de molde",
    "Piña",
    "Leche evaporada",
}
# Para cuántos días de uso queda cada insumo al final. Define qué acción le
# toca en la reposición: menos de dos días, comprar hoy; menos de siete,
# comprar esta semana; el resto, esperar. Queso y culantro además se pierden
# por merma, y les toca revisarla.
END_COVER_DAYS = {
    "Lomo de res": 0.7,
    "Cerveza Cusqueña 620 ml": 1.2,
    "Pechuga de pollo": 3.5,
    "Limón": 4.0,
    "Pescado fresco": 3.0,
    "Queso fresco": 6.0,
    "Culantro": 5.0,
}
# Lo que no está en la lista termina con una cobertura cómoda según cada cuánto
# se compra: los frescos no se guardan dos semanas.
DEFAULT_END_COVER = {2: 8.0, 4: 10.0, 7: 14.0}

WASTE_REASONS = (
    ("Se venció", "expiration"),
    ("Se malogró, olía mal", "expiration"),
    ("Estaba pasado, con hongos", "expiration"),
    ("La leche se cortó", "expiration"),
    ("Se pudrieron con el calor", "expiration"),
    ("Se cayó la bandeja al piso", "mishandling"),
    ("Se derramó al servir", "mishandling"),
    ("Se cortó la luz y se descongeló", "mishandling"),
    ("Quedó fuera del frío toda la noche", "mishandling"),
    ("El cliente lo devolvió, dijo que estaba frío", "customer_return"),
    ("Reclamo del cliente: estaba muy salado", "customer_return"),
    ("Se quemó en la plancha", "preparation_error"),
    ("Salió muy salado y se rehízo", "preparation_error"),
    ("Error de comanda, se preparó otro plato", "preparation_error"),
    ("Prueba de una receta nueva", "other"),
    ("Degustación para el personal", "other"),
)
WASTE_CANDIDATES = (
    "Pechuga de pollo",
    "Lomo de res",
    "Pescado fresco",
    "Tomate",
    "Cebolla roja",
    "Papa amarilla",
    "Leche evaporada",
    "Arroz",
    "Panceta de cerdo",
    "Limón",
)
# Los dos insumos que se pierden de verdad en el último mes.
HEAVY_WASTE = {
    "Queso fresco": (
        (6, 150, 260),
        ("Se venció el queso", "El queso se puso agrio", "Se malogró, la refrigeradora falló"),
    ),
    "Culantro": (
        (9, 60, 110),
        ("Se marchitó", "Culantro amarillo, se botó", "Se malogró con el calor"),
    ),
}


@dataclass(slots=True)
class DishInfo:
    id: int
    name: str
    price: Decimal
    recipe: list[tuple[int, Decimal]]


@dataclass(slots=True)
class PlannedItem:
    dish: DishInfo
    quantity: int
    notes: str = ""


@dataclass(slots=True)
class PlannedOrder:
    day: int
    business_date: date
    created_at: datetime
    waiter_id: int
    table_id: int | None
    customer_name: str
    notes: str
    items: list[PlannedItem]
    cancelled: bool
    payment_method: str | None = None
    number: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return sum((item.dish.price * item.quantity for item in self.items), Decimal("0.00"))


def _pick[T](rng: random.Random, options: tuple[tuple[T, int], ...] | dict[T, int]) -> T:
    pairs = list(options.items()) if isinstance(options, dict) else list(options)
    return rng.choices([option for option, _ in pairs], weights=[w for _, w in pairs])[0]


def _is_local_database(database_url: str) -> bool:
    return make_url(database_url).host in LOCAL_HOSTS


def _cash_received(rng: random.Random, total: Decimal) -> Decimal:
    """Lo que entrega el cliente: justo, o el billete que tenga a mano."""
    if rng.random() < 0.3:
        return total
    for bill in (Decimal(10), Decimal(20), Decimal(50), Decimal(100), Decimal(200)):
        if bill >= total and rng.random() < 0.6:
            return bill
    return (total / 10).to_integral_value(ROUND_CEILING) * 10


# -- Pedidos -----------------------------------------------------------------


def _plan_orders(
    rng: random.Random,
    start: date,
    zone: ZoneInfo,
    dishes: dict[str, DishInfo],
    waiters: list[tuple[int, int]],
    tables: list[int],
) -> list[PlannedOrder]:
    orders: list[PlannedOrder] = []
    for day in range(DAYS):
        business_date = start + timedelta(days=day)
        weekend = business_date.weekday() >= 5
        factor = (
            WEEKDAY_FACTOR[business_date.weekday()]
            * HOLIDAYS.get((business_date.month, business_date.day), 1.0)
            * (1 + GROWTH * day / DAYS)
            * rng.uniform(0.88, 1.12)
        )
        planned_today: list[PlannedOrder] = []
        for _ in range(round(BASE_ORDERS_PER_DAY * factor)):
            service = _pick(rng, SERVICES)
            hours = {"lunch": LUNCH_HOURS, "dinner": DINNER_HOURS, "off": OFF_HOURS}[service]
            opened = datetime.combine(
                business_date, time(_pick(rng, hours), rng.randrange(60)), tzinfo=zone
            ).astimezone(UTC)
            if service == "dinner":
                mains = MAINS["dinner"]
            elif weekend or service == "off":
                mains = MAINS["weekend_lunch"]
            else:
                mains = MAINS["weekday_lunch"]

            takeaway = rng.random() < TAKEAWAY_RATE
            party = 1 if takeaway and rng.random() < 0.6 else _pick(rng, PARTY_SIZES)
            chosen: dict[str, int] = defaultdict(int)
            for _ in range(party):
                chosen[_pick(rng, mains)] += 1
            if party >= 2 and rng.random() < 0.35:
                chosen[_pick(rng, STARTERS)] += 1
            drinkers = sum(1 for _ in range(party) if rng.random() < 0.65)
            while drinkers > 0:
                drink = _pick(rng, DRINKS)
                if drink == "Cerveza Cusqueña 620 ml" and service != "dinner" and not weekend:
                    drink = "Chicha morada (vaso)"
                chosen[drink] += 1
                drinkers -= 3 if drink in PITCHERS else 1

            items = [
                PlannedItem(
                    dish=dishes[name],
                    quantity=quantity,
                    notes=_pick(rng, ITEM_NOTES) if rng.random() < ITEM_NOTE_RATE else "",
                )
                for name, quantity in chosen.items()
                if name in dishes
            ]
            if not items:
                continue
            cancelled = rng.random() < CANCEL_RATE
            planned_today.append(
                PlannedOrder(
                    day=day,
                    business_date=business_date,
                    created_at=opened,
                    waiter_id=_pick(rng, waiters),
                    table_id=None if takeaway else rng.choice(tables),
                    customer_name=rng.choice(CUSTOMERS) if takeaway else "",
                    notes=_pick(rng, ORDER_NOTES) if rng.random() < ORDER_NOTE_RATE else "",
                    items=items,
                    cancelled=cancelled,
                    payment_method=None if cancelled else _pick(rng, PAYMENT_METHODS),
                )
            )
        # El correlativo del día sigue el orden en que se abrieron.
        planned_today.sort(key=lambda order: order.created_at)
        for number, order in enumerate(planned_today, start=1):
            order.number = number
        orders.extend(planned_today)
    return orders


def _order_row(rng: random.Random, restaurant_id: int, order: PlannedOrder) -> dict[str, Any]:
    total = order.total.quantize(CENT)
    closed = order.created_at + timedelta(minutes=rng.randint(40, 85))
    row: dict[str, Any] = {
        "restaurant_id": restaurant_id,
        "number": order.number,
        "business_date": order.business_date,
        "type": "takeaway" if order.table_id is None else "dine_in",
        "status": "cancelled" if order.cancelled else "paid",
        "table_id": order.table_id,
        "waiter_id": order.waiter_id,
        "customer_name": order.customer_name,
        "notes": order.notes,
        "total": total,
        "cancel_reason": rng.choice(CANCEL_REASONS) if order.cancelled else "",
        "payment_method": order.payment_method,
        "amount_received": (_cash_received(rng, total) if order.payment_method == "cash" else None),
        "created_at": order.created_at,
        "updated_at": closed,
        "paid_at": None if order.cancelled else closed,
        "cancelled_at": order.created_at + timedelta(minutes=rng.randint(8, 25))
        if order.cancelled
        else None,
    }
    return row


async def _insert_orders(
    session: AsyncSession, rng: random.Random, restaurant_id: int, orders: list[PlannedOrder]
) -> list[tuple[PlannedOrder, list[int]]]:
    """Inserta pedidos e ítems en bloque; devuelve cada pedido con los ids de sus ítems."""
    result: list[tuple[PlannedOrder, list[int]]] = []
    for offset in range(0, len(orders), 500):
        chunk = orders[offset : offset + 500]
        order_ids = (
            await session.scalars(
                insert(OrderRow).returning(OrderRow.id, sort_by_parameter_order=True),
                [_order_row(rng, restaurant_id, order) for order in chunk],
            )
        ).all()
        item_rows: list[dict[str, Any]] = []
        for order, order_id in zip(chunk, order_ids, strict=True):
            order.extra["id"] = order_id
            item_rows.extend(
                {
                    "restaurant_id": restaurant_id,
                    "order_id": order_id,
                    "menu_item_id": item.dish.id,
                    "name": item.dish.name,
                    "unit_price": item.dish.price,
                    "quantity": item.quantity,
                    "notes": item.notes,
                    "created_at": order.created_at,
                }
                for item in order.items
            )
        item_ids = list(
            (
                await session.scalars(
                    insert(OrderItemRow).returning(OrderItemRow.id, sort_by_parameter_order=True),
                    item_rows,
                )
            ).all()
        )
        cursor = 0
        for order in chunk:
            result.append((order, item_ids[cursor : cursor + len(order.items)]))
            cursor += len(order.items)
    return result


# -- Almacén -----------------------------------------------------------------


def _consumption(
    rng: random.Random,
    restaurant_id: int,
    inserted: list[tuple[PlannedOrder, list[int]]],
    costs: dict[int, Decimal],
    daily_use: dict[int, list[Decimal]],
) -> list[dict[str, Any]]:
    """Lo que gastó cada pedido servido, según la receta de cada plato."""
    rows: list[dict[str, Any]] = []
    for order, item_ids in inserted:
        if order.cancelled:
            continue
        served = order.created_at + timedelta(minutes=rng.randint(18, 35))
        for item, item_id in zip(order.items, item_ids, strict=True):
            for ingredient_id, per_portion in item.dish.recipe:
                amount = (per_portion * item.quantity).quantize(MILLI)
                daily_use[ingredient_id][order.day] += amount
                rows.append(
                    {
                        "restaurant_id": restaurant_id,
                        "ingredient_id": ingredient_id,
                        "kind": "consumption",
                        "quantity": -amount,
                        "unit_cost": costs[ingredient_id],
                        "reason": "",
                        "order_id": order.extra["id"],
                        "order_item_id": item_id,
                        "created_by": order.waiter_id,
                        "created_at": served,
                    }
                )
    return rows


def _plan_waste(
    rng: random.Random, by_name: dict[str, Ingredient], daily_use: dict[int, list[Decimal]]
) -> list[tuple[int, Ingredient, Decimal, str]]:
    """Mermas sueltas en todo el período y dos insumos que se pierden en el último mes."""
    wastes: list[tuple[int, Ingredient, Decimal, str]] = []
    for day in range(1, DAYS, 2):
        name = rng.choice([n for n in WASTE_CANDIDATES if n in by_name])
        ingredient = by_name[name]
        use = daily_use[ingredient.id or 0][day]
        if use <= 0:
            continue
        # Entre un 5 y un 15 % de lo que se usa en un día.
        amount = (use * Decimal(str(rng.uniform(0.05, 0.15)))).quantize(Decimal("1"))
        if ingredient.unit.value == "unit":
            amount = max(Decimal(1), amount)
        if amount > 0:
            wastes.append((day, ingredient, amount, rng.choice(WASTE_REASONS)[0]))
    for name, ((events, low, high), reasons) in HEAVY_WASTE.items():
        if name not in by_name:
            continue
        for day in sorted(rng.sample(range(DAYS - 28, DAYS), events)):
            wastes.append(
                (day, by_name[name], Decimal(rng.randint(low, high)), rng.choice(reasons))
            )
    return wastes


def _interval(name: str) -> tuple[int, int]:
    """Cada cuántos días se compra y cuántos días de colchón se dejan."""
    if name in FRESH:
        return 2, 1
    if name in EVERY_FEW_DAYS:
        return 4, 2
    return 7, 3


def _pack(unit: str) -> Decimal:
    return {"g": Decimal(250), "ml": Decimal(500)}.get(unit, Decimal(1))


def _round_up(amount: Decimal, step: Decimal) -> Decimal:
    return (amount / step).to_integral_value(ROUND_CEILING) * step


def _plan_purchases(
    ingredients: list[Ingredient],
    initial: dict[int, Decimal],
    daily_use: dict[int, list[Decimal]],
    daily_waste: dict[int, list[Decimal]],
) -> list[tuple[int, Ingredient, Decimal]]:
    """Compras como las haría el encargado, mirando lo que se viene.

    En cada día de compra se repone lo que se va a usar hasta la compra
    siguiente, más un colchón. La última compra del período, en cambio, deja
    el stock justo para los días de `END_COVER_DAYS`: así cada insumo termina
    en la situación que la demostración necesita.
    """
    purchases: list[tuple[int, Ingredient, Decimal]] = []
    for index, ingredient in enumerate(ingredients):
        ingredient_id = ingredient.id or 0
        interval, cushion = _interval(ingredient.name)
        offset = index % interval
        days = [day for day in range(DAYS) if (day - offset) % interval == 0]
        last_day = days[-1] if days else -1
        outflow = [
            use + waste
            for use, waste in zip(daily_use[ingredient_id], daily_waste[ingredient_id], strict=True)
        ]
        recent = sum(daily_use[ingredient_id][DAYS - 7 :], Decimal(0)) / 7
        target = Decimal(str(END_COVER_DAYS.get(ingredient.name, DEFAULT_END_COVER[interval])))
        stock = initial.get(ingredient_id, Decimal(0))
        for day in range(DAYS):
            if day in days:
                if day == last_day:
                    needed = sum(outflow[day:], Decimal(0)) + target * recent - stock
                    amount = _round_up(needed, Decimal(1))
                else:
                    horizon = min(DAYS, day + interval + cushion)
                    needed = sum(outflow[day:horizon], Decimal(0)) - stock
                    amount = _round_up(needed, _pack(ingredient.unit.value))
                if amount > 0:
                    purchases.append((day, ingredient, amount))
                    stock += amount
            stock -= outflow[day]
    return purchases


# -- Siembra -----------------------------------------------------------------


async def _synthetic_waiters(session: AsyncSession, restaurant_id: int) -> list[int] | None:
    """Crea los meseros sintéticos; `None` si ya existían (la historia ya está)."""
    users = SqlAlchemyUserRepository(session)
    if await users.get_by_email(SYNTHETIC_WAITERS[0].email) is not None:
        return None
    password_hash = BcryptPasswordHasher().hash(DEMO_PASSWORD)
    ids: list[int] = []
    for waiter in SYNTHETIC_WAITERS:
        created = await users.add(
            User(
                restaurant_id=restaurant_id,
                email=waiter.email,
                full_name=waiter.full_name,
                role=Role.WAITER,
                password_hash=password_hash,
            )
        )
        ids.append(created.id or 0)
    return ids


async def _kitchen_today(
    session: AsyncSession,
    rng: random.Random,
    restaurant_id: int,
    zone: ZoneInfo,
    dishes: dict[str, DishInfo],
    waiter_id: int,
    tables: list[int],
    now: datetime,
) -> int:
    """Tres pedidos en cocina ahora mismo, con notas, para ver el tablero y las alergias."""
    today = local_date(now, str(zone))
    last = await session.scalar(
        select(func.max(OrderRow.number)).where(
            OrderRow.restaurant_id == restaurant_id, OrderRow.business_date == today
        )
    )
    busy = set(
        (
            await session.scalars(
                select(OrderRow.table_id).where(
                    OrderRow.restaurant_id == restaurant_id,
                    OrderRow.status.in_(("open", "in_kitchen", "ready", "served")),
                    OrderRow.table_id.is_not(None),
                )
            )
        ).all()
    )
    free = [table for table in tables if table not in busy]
    plans = (
        (
            "",
            [
                ("Lomo saltado", 2, "uno sin cebolla"),
                ("Ceviche clásico", 1, "es alérgica a los mariscos, cambiar por pollo"),
            ],
        ),
        (
            "Mesa con un niño celíaco",
            [("Ají de gallina", 1, ""), ("Chicha morada (jarra 1 L)", 1, "")],
        ),
        ("", [("Arroz chaufa de pollo", 1, "alérgico al maní"), ("Inca Kola 500 ml", 1, "")]),
    )
    created = 0
    for index, (order_notes, lines) in enumerate(plans):
        table = free.pop(0) if free and index < 2 else None
        items = [(dishes[name], qty, note) for name, qty, note in lines if name in dishes]
        if not items:
            continue
        opened = now - timedelta(minutes=40 - 15 * index)
        order_id = await session.scalar(
            insert(OrderRow).returning(OrderRow.id),
            {
                "restaurant_id": restaurant_id,
                "number": (last or 0) + index + 1,
                "business_date": today,
                "type": "dine_in" if table else "takeaway",
                "status": "in_kitchen",
                "table_id": table,
                "waiter_id": waiter_id,
                "customer_name": "" if table else rng.choice(CUSTOMERS),
                "notes": order_notes,
                "total": sum((dish.price * qty for dish, qty, _ in items), Decimal("0.00")),
                "cancel_reason": "",
                "created_at": opened,
                "updated_at": opened + timedelta(minutes=2),
            },
        )
        await session.execute(
            insert(OrderItemRow),
            [
                {
                    "restaurant_id": restaurant_id,
                    "order_id": order_id,
                    "menu_item_id": dish.id,
                    "name": dish.name,
                    "unit_price": dish.price,
                    "quantity": qty,
                    "notes": note,
                    "created_at": opened,
                }
                for dish, qty, note in items
            ],
        )
        created += 1
    return created


async def seed() -> list[str]:
    report: list[str] = []
    rng = random.Random(SEED)
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        restaurant = await SqlAlchemyRestaurantRepository(session).get_by_slug(DEMO_SLUG)
        users = SqlAlchemyUserRepository(session)
        admin = await users.get_by_email(ADMIN_EMAIL)
        waiter = await users.get_by_email(WAITER_EMAIL)
        if restaurant is None or restaurant.id is None or admin is None or waiter is None:
            return ["Falta el restaurante de prueba: corre antes scripts/seed_dev.py."]
        restaurant_id = restaurant.id
        synthetic = await _synthetic_waiters(session, restaurant_id)
        if synthetic is None:
            return ["ya existía  histórico sintético (no se tocó nada)"]

        zone = ZoneInfo(restaurant.timezone)
        today = local_date(now, restaurant.timezone)
        start = today - timedelta(days=DAYS)
        dawn = datetime.combine(start, time(7, 0), tzinfo=zone).astimezone(UTC)

        items = await SqlAlchemyMenuRepository(session).list_items(restaurant_id)
        recipes = await SqlAlchemyRecipeRepository(session).lines_for_many(restaurant_id)
        dishes = {
            item.name: DishInfo(
                id=item.id or 0,
                name=item.name,
                price=item.price,
                recipe=[
                    (line.ingredient_id, line.quantity) for line in recipes.get(item.id or 0, [])
                ],
            )
            for item in items
            if item.is_active
        }
        ingredients = [
            ingredient
            for ingredient in await SqlAlchemyIngredientRepository(session).list_all(restaurant_id)
            if ingredient.is_active
        ]
        by_name = {ingredient.name: ingredient for ingredient in ingredients}
        costs = {ingredient.id or 0: ingredient.unit_cost for ingredient in ingredients}
        tables = [
            t.id or 0 for t in await SqlAlchemyTableRepository(session).list_all(restaurant_id)
        ]
        waiters = [
            (waiter.id or 0, 35),
            (synthetic[0], 35),
            (synthetic[1], 24),
            (admin.id or 0, 6),
        ]

        # El stock inicial de `seed_dev` pasa al primer día de la historia.
        await session.execute(
            update(StockMovementRow)
            .where(
                StockMovementRow.restaurant_id == restaurant_id,
                StockMovementRow.reason == "Stock inicial",
            )
            .values(created_at=dawn)
        )
        await session.execute(
            update(IngredientRow)
            .where(IngredientRow.restaurant_id == restaurant_id)
            .values(created_at=dawn)
        )
        initial = {
            int(ingredient_id): Decimal(str(total))
            for ingredient_id, total in (
                await session.execute(
                    select(StockMovementRow.ingredient_id, func.sum(StockMovementRow.quantity))
                    .where(StockMovementRow.restaurant_id == restaurant_id)
                    .group_by(StockMovementRow.ingredient_id)
                )
            ).all()
        }

        orders = _plan_orders(rng, start, zone, dishes, waiters, tables)
        inserted = await _insert_orders(session, rng, restaurant_id, orders)
        daily_use: dict[int, list[Decimal]] = defaultdict(lambda: [Decimal(0)] * DAYS)
        consumption = _consumption(rng, restaurant_id, inserted, costs, daily_use)

        wastes = _plan_waste(rng, by_name, daily_use)
        daily_waste: dict[int, list[Decimal]] = defaultdict(lambda: [Decimal(0)] * DAYS)
        for day, ingredient, amount, _ in wastes:
            daily_waste[ingredient.id or 0][day] += amount
        purchases = _plan_purchases(ingredients, initial, daily_use, daily_waste)

        def moment(day: int, hour: int, minute: int) -> datetime:
            local = datetime.combine(start + timedelta(days=day), time(hour, minute), tzinfo=zone)
            return local.astimezone(UTC)

        movements = (
            consumption
            + [
                {
                    "restaurant_id": restaurant_id,
                    "ingredient_id": ingredient.id,
                    "kind": "purchase",
                    "quantity": amount,
                    "unit_cost": ingredient.unit_cost,
                    "reason": PURCHASE_REASON,
                    "order_id": None,
                    "order_item_id": None,
                    "created_by": admin.id,
                    "created_at": moment(day, 7, rng.randrange(15, 55)),
                }
                for day, ingredient, amount in purchases
            ]
            + [
                {
                    "restaurant_id": restaurant_id,
                    "ingredient_id": ingredient.id,
                    "kind": "waste",
                    "quantity": -amount,
                    "unit_cost": ingredient.unit_cost,
                    "reason": reason,
                    "order_id": None,
                    "order_item_id": None,
                    "created_by": admin.id,
                    "created_at": moment(day, rng.choice((11, 16, 23)), rng.randrange(60)),
                }
                for day, ingredient, amount, reason in wastes
            ]
        )
        for offset in range(0, len(movements), 2000):
            await session.execute(insert(StockMovementRow), movements[offset : offset + 2000])

        in_kitchen = await _kitchen_today(
            session, rng, restaurant_id, zone, dishes, synthetic[0], tables, now
        )
        await session.commit()

        paid = [order for order in orders if not order.cancelled]
        sales = sum((order.total for order in paid), Decimal(0))
        report += [
            f"creados    2 meseros sintéticos (contraseña {DEMO_PASSWORD})",
            f"pedidos    {len(orders)} en {DAYS} días ({start} a {today - timedelta(days=1)}), "
            f"{len(orders) - len(paid)} cancelados, S/ {sales:.2f} vendidos",
            f"insumos    {len(consumption)} consumos, {len(purchases)} compras, "
            f"{len(wastes)} mermas",
            f"cocina     {in_kitchen} pedidos en curso hoy, con notas",
        ]
    await engine.dispose()
    return report


def main() -> int:
    settings = get_settings()
    if not settings.debug:
        print("DEBUG está desactivado. Los datos sintéticos solo se siembran en desarrollo.")
        return 1
    if not _is_local_database(settings.database_url):
        print("La base configurada no es local. Los datos sintéticos no se siembran ahí.")
        return 1
    for line in asyncio.run(seed()):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
