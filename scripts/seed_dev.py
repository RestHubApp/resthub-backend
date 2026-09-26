"""Siembra un restaurante de prueba completo en la base de desarrollo.

Existe para poder abrir las pantallas de cada rol sin crear a mano un
restaurante, su personal, su carta y su almacén. Además del encargado y el
mesero, siembra un rol propio (Cocinero) con su cuenta, para ver un rol armado
por el local, y una cuenta de la administración del sistema para entrar a
`/plataforma`. Las cuentas comparten la contraseña `DEMO_PASSWORD`.

Siembra un restaurante peruano chico: veinte platos en cinco categorías, ocho
mesas, unos treinta insumos con stock inicial (cargado como compras, para que
el libro de movimientos cuadre) y recetas para casi todos los platos. Dos
platos quedan sin receta a propósito, para ver cómo se muestra un costo
desconocido, y un par de insumos arrancan por debajo del mínimo, para ver las
alertas.

Es idempotente: lo que ya existe se deja como está. Se niega a correr salvo que
se cumplan dos condiciones a la vez: la base es local y `DEBUG` está activo.
Mirar solo el host no alcanza: un túnel SSH a la base de producción también se
ve como `127.0.0.1`, y sembraría ahí cuentas con una contraseña que está
escrita en este archivo. Un despliegue corre con `DEBUG=false`, así que la
segunda condición lo deja afuera aunque la base parezca local. El entorno de
demostración desplegado lo habilita a propósito con `ALLOW_DEMO_SEED=true`.

Uso:
    uv run python scripts/seed_dev.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.config import get_settings
from resthub.core.database import SessionFactory, engine
from resthub.core.permissions import Permission, RoleKind
from resthub.core.security import BcryptPasswordHasher
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
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain.entities import Restaurant

DEMO_PASSWORD = "resthub123"
DEMO_RESTAURANT = Restaurant(name="Restaurante Demo", slug="restaurante-demo")

# SQLite no tiene host; Postgres local se escribe de cualquiera de estas formas.
LOCAL_HOSTS = {None, "localhost", "127.0.0.1", "::1"}


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
class DemoAccount:
    email: str
    full_name: str
    # Por nombre: el identificador del rol depende de la base.
    role_name: str


ACCOUNTS = (
    DemoAccount("admin@resthub.dev", "Encargado Demo", OWNER_ROLE_NAME),
    DemoAccount("mesero@resthub.dev", "Mesero Demo", WAITER_ROLE_NAME),
    DemoAccount("cocina@resthub.dev", "Cocinero Demo", COOK_ROLE_NAME),
)


# No es del restaurante demo: es de la plataforma, que administra a todos.
PLATFORM_ADMIN_EMAIL = "plataforma@resthub.dev"
PLATFORM_ADMIN_NAME = "Administración RestHub"

TABLES = tuple(str(number) for number in range(1, 9))


@dataclass(frozen=True, slots=True)
class DemoIngredient:
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
    DemoIngredient(*row)
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
class DemoDish:
    name: str
    price: str
    description: str = ""
    # (insumo, cantidad por porción). Vacía: el plato queda sin receta.
    recipe: tuple[tuple[str, str], ...] = ()


# Categorías en el orden de la carta, cada una con sus platos.
MENU: tuple[tuple[str, tuple[DemoDish, ...]], ...] = (
    (
        "Menú del día",
        (
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish("Choclo con queso", "10.00", "Choclo tierno con queso fresco."),
        ),
    ),
    (
        "Marinos",
        (
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish(
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
            DemoDish(
                "Limonada (jarra 1 L)",
                "14.00",
                "",
                (("Limón", "8"), ("Azúcar", "90")),
            ),
            DemoDish("Inca Kola 500 ml", "5.00", "", (("Inca Kola 500 ml", "1"),)),
            DemoDish("Coca-Cola 500 ml", "5.00", "", (("Coca-Cola 500 ml", "1"),)),
            DemoDish("Agua mineral", "3.50", "", (("Agua mineral 625 ml", "1"),)),
            DemoDish("Cerveza Cusqueña 620 ml", "10.00", "", (("Cerveza Cusqueña 620 ml", "1"),)),
            DemoDish("Emoliente", "3.00", "Caliente, del puesto de la esquina."),
        ),
    ),
)


def _is_local_database(database_url: str) -> bool:
    return make_url(database_url).host in LOCAL_HOSTS


async def _seed_restaurant(session: AsyncSession, report: list[str]) -> int:
    restaurants = SqlAlchemyRestaurantRepository(session)
    existing = await restaurants.get_by_slug(DEMO_RESTAURANT.slug)
    if existing is not None and existing.id is not None:
        report.append(f"ya existía  restaurante {existing.slug}")
        return existing.id
    created = await restaurants.add(
        Restaurant(name=DEMO_RESTAURANT.name, slug=DEMO_RESTAURANT.slug)
    )
    report.append(f"creado      restaurante {created.slug}")
    return created.id or 0


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


async def _seed_accounts(session: AsyncSession, restaurant_id: int, report: list[str]) -> None:
    roles = await _seed_roles(session, restaurant_id, report)
    password_hash = BcryptPasswordHasher().hash(DEMO_PASSWORD)
    users = SqlAlchemyUserRepository(session)
    for account in ACCOUNTS:
        if await users.get_by_email(account.email) is not None:
            report.append(f"ya existía  {account.email}")
            continue
        await users.add(
            User(
                restaurant_id=restaurant_id,
                email=account.email,
                full_name=account.full_name,
                role=roles[account.role_name],
                password_hash=password_hash,
            )
        )
        report.append(f"creada      {account.email} ({account.role_name})")


async def _seed_platform_admin(session: AsyncSession, report: list[str]) -> None:
    admins = SqlAlchemyPlatformAdminRepository(session)
    if await admins.get_by_email(PLATFORM_ADMIN_EMAIL) is not None:
        report.append(f"ya existía  {PLATFORM_ADMIN_EMAIL} (plataforma)")
        return
    await admins.add(
        PlatformAdmin(
            email=PLATFORM_ADMIN_EMAIL,
            full_name=PLATFORM_ADMIN_NAME,
            password_hash=BcryptPasswordHasher().hash(DEMO_PASSWORD),
        )
    )
    report.append(f"creada      {PLATFORM_ADMIN_EMAIL} (plataforma)")


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


async def seed() -> list[str]:
    report: list[str] = []
    async with SessionFactory() as session:
        restaurant_id = await _seed_restaurant(session, report)
        await _seed_accounts(session, restaurant_id, report)
        admin = await SqlAlchemyUserRepository(session).get_by_email(ACCOUNTS[0].email)
        admin_id = admin.id if admin is not None and admin.id is not None else 0
        await _seed_tables(session, restaurant_id, report)
        dish_ids = await _seed_menu(session, restaurant_id, report)
        ingredient_ids = await _seed_ingredients(session, restaurant_id, admin_id, report)
        await _seed_recipes(session, restaurant_id, dish_ids, ingredient_ids, report)
        # La cuenta de plataforma no tiene restaurante: con su contraseña
        # pública, en un entorno demo desplegado cualquiera administraría todos
        # los locales. Solo se siembra en desarrollo local.
        settings = get_settings()
        if settings.debug and _is_local_database(settings.database_url):
            await _seed_platform_admin(session, report)
        else:
            report.append(
                "plataforma  no se siembra fuera de desarrollo local: "
                "usa scripts/create_platform_admin.py"
            )
        await session.commit()
    await engine.dispose()
    return report


def main() -> int:
    settings = get_settings()
    if settings.allow_demo_seed:
        # Lo pidió quien configuró el entorno: es el de demostración.
        print("ALLOW_DEMO_SEED activo: se siembra aunque la base no sea local.")
    elif not settings.debug:
        print("DEBUG está desactivado. Los datos de prueba solo se siembran en desarrollo.")
        return 1
    elif not _is_local_database(settings.database_url):
        print("La base configurada no es local. Los datos de prueba no se siembran ahí.")
        return 1
    for line in asyncio.run(seed()):
        print(line)
    print(f"Contraseña de todas las cuentas: {DEMO_PASSWORD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
