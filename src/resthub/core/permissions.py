"""Catálogo de permisos y el mapa fijo de rol a permisos.

Un permiso es una acción que el servidor sabe comprobar: cada endpoint exige
uno. La lista es fija y vive en el código, porque cada permiso nuevo necesita un
endpoint que lo exija. Qué rol tiene cuáles también es fijo por ahora: con dos
tipos de cuenta, una pantalla para editar roles sería más superficie que
beneficio. Si algún día hace falta, el catálogo ya tiene las etiquetas que esa
pantalla mostraría.

Python puro: lo importan los casos de uso y el núcleo de autenticación.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from resthub.core.identity import Role


class Permission(StrEnum):
    MENU_READ = "menu.read"
    MENU_MANAGE = "menu.manage"
    TABLES_READ = "tables.read"
    TABLES_MANAGE = "tables.manage"
    ORDERS_TAKE = "orders.take"
    ORDERS_READ_ALL = "orders.read_all"
    ORDERS_MANAGE = "orders.manage"
    ORDERS_CHARGE = "orders.charge"
    ORDERS_DISCOUNT_ANY = "orders.discount_any"
    CASH_MANAGE = "cash.manage"
    BILLING_ISSUE = "billing.issue"
    BILLING_MANAGE = "billing.manage"
    CUSTOMERS_READ = "customers.read"
    CUSTOMERS_MANAGE = "customers.manage"
    RESERVATIONS_READ = "reservations.read"
    RESERVATIONS_MANAGE = "reservations.manage"
    INVENTORY_READ = "inventory.read"
    INVENTORY_MANAGE = "inventory.manage"
    STAFF_MANAGE = "staff.manage"
    RESTAURANT_MANAGE = "restaurant.manage"
    INSIGHTS_READ = "insights.read"
    ACTIVITY_READ = "activity.read"


@dataclass(frozen=True, slots=True)
class PermissionInfo:
    label: str
    group: str


_MENU = "Menú"
_MESAS = "Mesas"
_PEDIDOS = "Pedidos"
_CAJA = "Caja"
_INVENTARIO = "Inventario"
_CLIENTES = "Clientes y reservas"
_ADMINISTRACION = "Administración"

# El orden en que la interfaz muestra los grupos.
PERMISSION_GROUPS: tuple[str, ...] = (
    _MENU,
    _MESAS,
    _PEDIDOS,
    _CAJA,
    _CLIENTES,
    _INVENTARIO,
    _ADMINISTRACION,
)

CATALOG: dict[Permission, PermissionInfo] = {
    Permission.MENU_READ: PermissionInfo("Ver el menú", _MENU),
    Permission.MENU_MANAGE: PermissionInfo("Editar categorías, platos y precios", _MENU),
    Permission.TABLES_READ: PermissionInfo("Ver las mesas", _MESAS),
    Permission.TABLES_MANAGE: PermissionInfo("Agregar y editar mesas", _MESAS),
    Permission.ORDERS_TAKE: PermissionInfo(
        "Tomar pedidos, agregar platos y marcarlos servidos", _PEDIDOS
    ),
    Permission.ORDERS_READ_ALL: PermissionInfo("Ver todos los pedidos del local", _PEDIDOS),
    Permission.ORDERS_MANAGE: PermissionInfo(
        "Cambiar el estado en cocina y cancelar pedidos", _PEDIDOS
    ),
    Permission.ORDERS_CHARGE: PermissionInfo(
        "Cobrar pedidos y dar descuentos hasta el tope del mesero", _CAJA
    ),
    Permission.ORDERS_DISCOUNT_ANY: PermissionInfo(
        "Dar descuentos sin tope e invitar platos (cortesías)", _CAJA
    ),
    Permission.CASH_MANAGE: PermissionInfo("Abrir y cerrar la caja y ver sus arqueos", _CAJA),
    Permission.BILLING_ISSUE: PermissionInfo("Emitir boletas y facturas de lo cobrado", _CAJA),
    Permission.BILLING_MANAGE: PermissionInfo(
        "Configurar los datos fiscales y revisar los comprobantes", _CAJA
    ),
    Permission.CUSTOMERS_READ: PermissionInfo("Buscar clientes y ver su ficha", _CLIENTES),
    Permission.CUSTOMERS_MANAGE: PermissionInfo("Dar de alta y editar clientes", _CLIENTES),
    Permission.RESERVATIONS_READ: PermissionInfo("Ver las reservas del día", _CLIENTES),
    Permission.RESERVATIONS_MANAGE: PermissionInfo("Tomar, editar y cerrar reservas", _CLIENTES),
    Permission.INVENTORY_READ: PermissionInfo("Ver insumos y stock", _INVENTARIO),
    Permission.INVENTORY_MANAGE: PermissionInfo(
        "Registrar compras, mermas, ajustes y recetas", _INVENTARIO
    ),
    Permission.STAFF_MANAGE: PermissionInfo(
        "Dar de alta y administrar al personal", _ADMINISTRACION
    ),
    Permission.RESTAURANT_MANAGE: PermissionInfo(
        "Editar los datos del restaurante", _ADMINISTRACION
    ),
    Permission.INSIGHTS_READ: PermissionInfo("Ver indicadores y predicciones", _ADMINISTRACION),
    Permission.ACTIVITY_READ: PermissionInfo("Ver la bitácora de movimientos", _ADMINISTRACION),
}

# Lo que hace un mesero desde el celular: ver el menú y las mesas, tomar
# pedidos y cobrarlos (el mesero hace de cajero). Qué pedidos ve y cuáles cobra
# (los que tomó) lo decide el caso de uso, no un permiso.
_WAITER = frozenset(
    {
        Permission.MENU_READ,
        Permission.TABLES_READ,
        Permission.ORDERS_TAKE,
        Permission.ORDERS_CHARGE,
        Permission.BILLING_ISSUE,
        # Atiende el teléfono: toma deliveries y reservas, registra clientes.
        Permission.CUSTOMERS_READ,
        Permission.CUSTOMERS_MANAGE,
        Permission.RESERVATIONS_READ,
        Permission.RESERVATIONS_MANAGE,
    }
)

# El encargado hace todo lo del mesero y además administra. Se arma con el
# catálogo entero y no con una lista aparte para que un permiso nuevo no quede
# huérfano por olvidarse de sumarlo acá.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.WAITER: _WAITER,
}


def permissions_for(role: Role) -> frozenset[Permission]:
    """Los permisos de un tipo de cuenta."""
    return ROLE_PERMISSIONS[role]
