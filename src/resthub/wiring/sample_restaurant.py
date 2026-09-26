"""Los datos de muestra de un restaurante: carta peruana, mesas, insumos, recetas y personal.

Los usan dos caminos y por eso viven en un solo lugar:

- `scripts/seed_dev.py`, que siembra el "Restaurante Demo" de desarrollo con
  cuentas `@resthub.dev` y una contraseña conocida.
- El reinicio del local de muestra de la vista previa (`wiring/sandbox.py`),
  que siembra el "Restaurante de muestra" con cuentas sin contraseña
  utilizable.

Lo que cambia entre ambos (el restaurante, las cuentas y su contraseña) lo
pasa quien llama; las tablas de datos son las mismas.

Es parte de la raíz de composición porque cruza módulos: escribe con los
repositorios de `accounts`, `menu`, `orders` e `inventory`. Todo va en la sesión
que recibe y no confirma nada: la transacción es de quien llama.

Siembra un restaurante peruano chico: veinte platos en cinco categorías, ocho
mesas, unos treinta insumos con stock inicial (cargado como compras, para que
el libro de movimientos cuadre) y recetas para casi todos los platos. Dos
platos quedan sin receta a propósito, para ver cómo se muestra un costo
desconocido, y un par de insumos arrancan por debajo del mínimo, para ver las
alertas. Además de los roles Encargado y Mesero, crea un rol propio
(Cocinero), para ver un rol armado por el local.

Es idempotente: lo que ya existe se deja como está.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.permissions import Permission, RoleKind
from resthub.modules.accounts.adapters.persistence.sqlalchemy_role_repository import (
    SqlAlchemyRoleRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.roles import OWNER_ROLE_NAME, WAITER_ROLE_NAME, Role
from resthub.modules.accounts.use_cases.manage_roles import ensure_base_roles
from resthub.modules.inventory.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyIngredientRepository,
    SqlAlchemyRecipeRepository,
    SqlAlchemyStockLedger,
)
from resthub.modules.inventory.domain.entities import (
    Ingredient,
    MovementKind,
    RecipeLine,
    StockMovement,
    Unit,
)
from resthub.modules.menu.adapters.persistence.sqlalchemy_menu_repository import (
    SqlAlchemyMenuRepository,
)
from resthub.modules.menu.domain.entities import MenuCategory, MenuItem
from resthub.modules.orders.adapters.persistence.sqlalchemy_table_repository import (
    SqlAlchemyTableRepository,
)
from resthub.modules.orders.domain.tables import DiningTable

COOK_ROLE_NAME = "Cocinero"
# Ve la carta y el tablero de cocina y mueve los pedidos; no cobra.
COOK_PERMISSIONS = frozenset(
    {
        Permission.MENU_READ,
        Permission.ORDERS_READ_ALL,
        Permission.ORDERS_MANAGE,
        Permission.INVENTORY_READ,
    }
)


@dataclass(frozen=True, slots=True)
class SampleAccount:
    email: str
    full_name: str
    # Por nombre: el identificador del rol depende de la base.
    role_name: str


def sample_accounts(
    emails: tuple[str, str, str], names: tuple[str, str, str]
) -> tuple[SampleAccount, ...]:
    """Un encargado, un mesero y un cocinero, en ese orden."""
    return tuple(
        SampleAccount(email, name, role)
        for email, name, role in zip(
            emails, names, (OWNER_ROLE_NAME, WAITER_ROLE_NAME, COOK_ROLE_NAME), strict=True
        )
    )


TABLES = tuple(str(number) for number in range(1, 9))


@dataclass(frozen=True, slots=True)
class SampleIngredient:
    name: str
    unit: Unit
    min_stock: str
    # Costo por unidad del insumo: por gramo, por mililitro o por unidad.
    unit_cost: str
    # Lo que entra como compra inicial.
    initial_stock: str


# (nombre, unidad, mínimo, costo unitario, compra inicial). Culantro y
# Cusqueña arrancan por debajo del mínimo para que haya alertas que mirar.
INGREDIENTS = tuple(
    SampleIngredient(*row)
    for row in (
        ("Arroz", Unit.GRAM, "5000", "0.0042", "20000"),
        ("Pechuga de pollo", Unit.GRAM, "3000", "0.0145", "10000"),
        ("Lomo de res", Unit.GRAM, "2000", "0.042", "6000"),
        ("Pescado fresco", Unit.GRAM, "2000", "0.028", "6000"),
        ("Panceta de cerdo", Unit.GRAM, "2000", "0.022", "5000"),
        ("Papa amarilla", Unit.GRAM, "3000", "0.0038", "10000"),
        ("Papa blanca", Unit.GRAM, "3000", "0.0025", "12000"),
        ("Camote", Unit.GRAM, "1000", "0.003", "4000"),
        ("Cebolla roja", Unit.GRAM, "2000", "0.0028", "8000"),
        ("Tomate", Unit.GRAM, "1000", "0.0035", "4000"),
        ("Limón", Unit.UNIT, "50", "0.25", "200"),
        ("Ají amarillo en pasta", Unit.GRAM, "500", "0.018", "2000"),
        ("Ají limo", Unit.GRAM, "200", "0.02", "500"),
        ("Culantro", Unit.GRAM, "200", "0.012", "150"),
        ("Choclo desgranado", Unit.GRAM, "1000", "0.008", "3000"),
        ("Queso fresco", Unit.GRAM, "500", "0.024", "2000"),
        ("Leche evaporada", Unit.MILLILITER, "1000", "0.0085", "4000"),
        ("Pan de molde", Unit.UNIT, "10", "0.35", "40"),
        ("Huevo", Unit.UNIT, "30", "0.5", "90"),
        ("Cebolla china", Unit.GRAM, "300", "0.01", "1000"),
        ("Sillao", Unit.MILLILITER, "500", "0.012", "2000"),
        ("Aceite vegetal", Unit.MILLILITER, "2000", "0.009", "10000"),
        ("Maíz morado", Unit.GRAM, "1000", "0.006", "4000"),
        ("Azúcar", Unit.GRAM, "2000", "0.0038", "8000"),
        ("Piña", Unit.UNIT, "3", "4.5", "8"),
        ("Inca Kola 500 ml", Unit.UNIT, "24", "2.2", "48"),
        ("Coca-Cola 500 ml", Unit.UNIT, "24", "2.2", "48"),
        ("Agua mineral 625 ml", Unit.UNIT, "24", "1.1", "36"),
        ("Cerveza Cusqueña 620 ml", Unit.UNIT, "12", "5.5", "10"),
    )
)


@dataclass(frozen=True, slots=True)
class SampleDish:
    name: str
    price: str
    description: str = ""
    # (insumo, cantidad por porción). Vacía: el plato queda sin receta.
    recipe: tuple[tuple[str, str], ...] = ()


# Categorías en el orden de la carta, cada una con sus platos.
MENU: tuple[tuple[str, tuple[SampleDish, ...]], ...] = (
    (
        "Menú del día",
        (
            SampleDish(
                "Menú del día",
                "15.00",
                "Entrada, segundo y refresco. Pregunta por el de hoy.",
                (
                    ("Arroz", "150"),
                    ("Pechuga de pollo", "120"),
                    ("Papa blanca", "100"),
                    ("Cebolla roja", "30"),
                    ("Aceite vegetal", "15"),
                ),
            ),
        ),
    ),
    (
        "Entradas",
        (
            SampleDish(
                "Papa a la huancaína",
                "12.00",
                "Papa amarilla con crema de ají amarillo y queso fresco.",
                (
                    ("Papa amarilla", "250"),
                    ("Queso fresco", "50"),
                    ("Ají amarillo en pasta", "25"),
                    ("Leche evaporada", "40"),
                    ("Huevo", "0.5"),
                    ("Aceite vegetal", "10"),
                ),
            ),
            SampleDish(
                "Causa limeña",
                "14.00",
                "Papa amarilla prensada rellena de pollo.",
                (
                    ("Papa amarilla", "200"),
                    ("Pechuga de pollo", "60"),
                    ("Ají amarillo en pasta", "15"),
                    ("Limón", "0.5"),
                    ("Aceite vegetal", "10"),
                ),
            ),
            SampleDish(
                "Leche de tigre",
                "15.00",
                "El jugo del ceviche, con trocitos de pescado.",
                (
                    ("Pescado fresco", "80"),
                    ("Limón", "3"),
                    ("Cebolla roja", "30"),
                    ("Ají limo", "5"),
                    ("Culantro", "3"),
                ),
            ),
            SampleDish("Choclo con queso", "10.00", "Choclo tierno con queso fresco."),
        ),
    ),
    (
        "Marinos",
        (
            SampleDish(
                "Ceviche clásico",
                "32.00",
                "Pescado del día en limón, con camote y choclo.",
                (
                    ("Pescado fresco", "250"),
                    ("Limón", "6"),
                    ("Cebolla roja", "80"),
                    ("Ají limo", "8"),
                    ("Culantro", "5"),
                    ("Camote", "120"),
                    ("Choclo desgranado", "60"),
                ),
            ),
            SampleDish(
                "Chicharrón de pescado",
                "30.00",
                "Trozos de pescado fritos con sarsa criolla.",
                (
                    ("Pescado fresco", "250"),
                    ("Aceite vegetal", "60"),
                    ("Limón", "2"),
                    ("Cebolla roja", "40"),
                ),
            ),
        ),
    ),
    (
        "Fondos",
        (
            SampleDish(
                "Lomo saltado",
                "32.00",
                "Lomo al wok con cebolla, tomate, papas fritas y arroz.",
                (
                    ("Lomo de res", "200"),
                    ("Papa blanca", "250"),
                    ("Cebolla roja", "100"),
                    ("Tomate", "100"),
                    ("Arroz", "150"),
                    ("Sillao", "15"),
                    ("Aceite vegetal", "40"),
                    ("Ají amarillo en pasta", "10"),
                ),
            ),
            SampleDish(
                "Ají de gallina",
                "24.00",
                "Pollo deshilachado en crema de ají amarillo, con arroz y papa.",
                (
                    ("Pechuga de pollo", "180"),
                    ("Ají amarillo en pasta", "40"),
                    ("Leche evaporada", "80"),
                    ("Pan de molde", "2"),
                    ("Arroz", "150"),
                    ("Papa amarilla", "120"),
                    ("Huevo", "0.5"),
                ),
            ),
            SampleDish(
                "Arroz chaufa de pollo",
                "22.00",
                "Arroz salteado al estilo chifa.",
                (
                    ("Arroz", "250"),
                    ("Pechuga de pollo", "120"),
                    ("Huevo", "1"),
                    ("Cebolla china", "30"),
                    ("Sillao", "20"),
                    ("Aceite vegetal", "25"),
                ),
            ),
            SampleDish(
                "Chicharrón de cerdo",
                "28.00",
                "Panceta crocante con camote frito y sarsa.",
                (
                    ("Panceta de cerdo", "300"),
                    ("Camote", "150"),
                    ("Cebolla roja", "60"),
                    ("Limón", "1"),
                    ("Aceite vegetal", "20"),
                ),
            ),
            SampleDish(
                "Pollo saltado",
                "24.00",
                "Como el lomo saltado, con pollo.",
                (
                    ("Pechuga de pollo", "200"),
                    ("Papa blanca", "250"),
                    ("Cebolla roja", "100"),
                    ("Tomate", "100"),
                    ("Arroz", "150"),
                    ("Sillao", "15"),
                    ("Aceite vegetal", "40"),
                ),
            ),
        ),
    ),
    (
        "Bebidas",
        (
            SampleDish(
                "Chicha morada (vaso)",
                "5.00",
                "Preparada en casa.",
                (
                    ("Maíz morado", "30"),
                    ("Azúcar", "25"),
                    ("Piña", "0.05"),
                    ("Limón", "0.25"),
                ),
            ),
            SampleDish(
                "Chicha morada (jarra 1 L)",
                "15.00",
                "Preparada en casa.",
                (
                    ("Maíz morado", "100"),
                    ("Azúcar", "90"),
                    ("Piña", "0.15"),
                    ("Limón", "1"),
                ),
            ),
            SampleDish(
                "Limonada (jarra 1 L)",
                "14.00",
                "",
                (("Limón", "8"), ("Azúcar", "90")),
            ),
            SampleDish("Inca Kola 500 ml", "5.00", "", (("Inca Kola 500 ml", "1"),)),
            SampleDish("Coca-Cola 500 ml", "5.00", "", (("Coca-Cola 500 ml", "1"),)),
            SampleDish("Agua mineral", "3.50", "", (("Agua mineral 625 ml", "1"),)),
            SampleDish("Cerveza Cusqueña 620 ml", "10.00", "", (("Cerveza Cusqueña 620 ml", "1"),)),
            SampleDish("Emoliente", "3.00", "Caliente, del puesto de la esquina."),
        ),
    ),
)


async def _seed_roles(
    session: AsyncSession, restaurant_id: int, report: list[str]
) -> dict[str, Role]:
    """Los roles base y el del cocinero, por nombre."""
    roles = SqlAlchemyRoleRepository(session)
    await ensure_base_roles(roles, restaurant_id)
    by_name = {role.name: role for role in await roles.list_for_restaurant(restaurant_id)}
    if COOK_ROLE_NAME in by_name:
        report.append(f"ya existía  rol {COOK_ROLE_NAME}")
    else:
        by_name[COOK_ROLE_NAME] = await roles.add(
            Role(
                restaurant_id=restaurant_id,
                name=COOK_ROLE_NAME,
                kind=RoleKind.CUSTOM,
                stored_permissions=COOK_PERMISSIONS,
            )
        )
        report.append(f"creado      rol {COOK_ROLE_NAME}")
    return by_name


async def _seed_accounts(
    session: AsyncSession,
    restaurant_id: int,
    accounts: Sequence[SampleAccount],
    password_hash: str,
    report: list[str],
) -> int:
    """Las cuentas del personal; devuelve el identificador del encargado."""
    roles = await _seed_roles(session, restaurant_id, report)
    users = SqlAlchemyUserRepository(session)
    owner_id = 0
    for account in accounts:
        user = await users.get_by_email(account.email)
        if user is not None:
            report.append(f"ya existía  {account.email}")
        else:
            user = await users.add(
                User(
                    restaurant_id=restaurant_id,
                    email=account.email,
                    full_name=account.full_name,
                    role=roles[account.role_name],
                    password_hash=password_hash,
                )
            )
            report.append(f"creada      {account.email} ({account.role_name})")
        if account.role_name == OWNER_ROLE_NAME and not owner_id:
            owner_id = user.id or 0
    return owner_id


async def _seed_tables(session: AsyncSession, restaurant_id: int, report: list[str]) -> None:
    tables = SqlAlchemyTableRepository(session)
    created = 0
    for position, label in enumerate(TABLES):
        if await tables.find_by_label(restaurant_id, label) is None:
            await tables.add(
                DiningTable(restaurant_id=restaurant_id, label=label, position=position)
            )
            created += 1
    report.append(f"mesas       {created} creadas, {len(TABLES) - created} ya existían")


async def _seed_menu(
    session: AsyncSession, restaurant_id: int, report: list[str]
) -> dict[str, int]:
    """Categorías y platos; devuelve el identificador de cada plato por nombre."""
    menu = SqlAlchemyMenuRepository(session)
    dish_ids: dict[str, int] = {}
    created = 0
    for category_position, (category_name, dishes) in enumerate(MENU):
        category = await menu.find_category_by_name(restaurant_id, category_name)
        if category is None:
            category = await menu.add_category(
                MenuCategory(
                    restaurant_id=restaurant_id, name=category_name, position=category_position
                )
            )
        for position, dish in enumerate(dishes):
            item = await menu.find_item_by_name(restaurant_id, dish.name)
            if item is None:
                item = await menu.add_item(
                    MenuItem(
                        restaurant_id=restaurant_id,
                        category_id=category.id or 0,
                        name=dish.name,
                        price=Decimal(dish.price),
                        description=dish.description,
                        position=position,
                    )
                )
                created += 1
            dish_ids[dish.name] = item.id or 0
    report.append(f"platos      {created} creados, {len(dish_ids) - created} ya existían")
    return dish_ids


async def _seed_ingredients(
    session: AsyncSession, restaurant_id: int, admin_id: int, report: list[str]
) -> dict[str, int]:
    """Insumos con su compra inicial; devuelve el identificador de cada uno por nombre.

    La compra inicial solo se registra al crear el insumo: volver a correr el
    script no duplica el stock.
    """
    ingredients = SqlAlchemyIngredientRepository(session)
    ledger = SqlAlchemyStockLedger(session)
    ids: dict[str, int] = {}
    created = 0
    for demo in INGREDIENTS:
        ingredient = await ingredients.find_by_name(restaurant_id, demo.name)
        if ingredient is None:
            ingredient = await ingredients.add(
                Ingredient(
                    restaurant_id=restaurant_id,
                    name=demo.name,
                    unit=demo.unit,
                    min_stock=Decimal(demo.min_stock),
                    unit_cost=Decimal(demo.unit_cost),
                )
            )
            await ledger.add(
                StockMovement(
                    restaurant_id=restaurant_id,
                    ingredient_id=ingredient.id or 0,
                    kind=MovementKind.PURCHASE,
                    quantity=Decimal(demo.initial_stock),
                    unit_cost=Decimal(demo.unit_cost),
                    reason="Stock inicial",
                    created_by=admin_id,
                )
            )
            created += 1
        ids[demo.name] = ingredient.id or 0
    report.append(f"insumos     {created} creados, {len(ids) - created} ya existían")
    return ids


async def _seed_recipes(
    session: AsyncSession,
    restaurant_id: int,
    dish_ids: dict[str, int],
    ingredient_ids: dict[str, int],
    report: list[str],
) -> None:
    recipes = SqlAlchemyRecipeRepository(session)
    created = 0
    for _, dishes in MENU:
        for dish in dishes:
            menu_item_id = dish_ids[dish.name]
            # Una receta que ya existe no se pisa: pudo haberla corregido
            # alguien desde la pantalla.
            if not dish.recipe or await recipes.lines_for(restaurant_id, menu_item_id):
                continue
            await recipes.replace(
                restaurant_id,
                menu_item_id,
                [
                    RecipeLine(ingredient_id=ingredient_ids[name], quantity=Decimal(quantity))
                    for name, quantity in dish.recipe
                ],
            )
            created += 1
    report.append(f"recetas     {created} creadas")


async def seed_sample_restaurant(
    session: AsyncSession,
    restaurant_id: int,
    accounts: Sequence[SampleAccount],
    password_hash: str,
    report: list[str] | None = None,
) -> None:
    """Roles, cuentas, mesas, carta, insumos con su stock inicial y recetas.

    `password_hash` es el de todas las cuentas: el de una contraseña conocida
    en desarrollo, el de un valor aleatorio descartado en el local de muestra.
    El stock inicial queda a nombre del encargado de `accounts`.
    """
    lines = report if report is not None else []
    owner_id = await _seed_accounts(session, restaurant_id, accounts, password_hash, lines)
    await _seed_tables(session, restaurant_id, lines)
    dish_ids = await _seed_menu(session, restaurant_id, lines)
    ingredient_ids = await _seed_ingredients(session, restaurant_id, owner_id, lines)
    await _seed_recipes(session, restaurant_id, dish_ids, ingredient_ids, lines)
