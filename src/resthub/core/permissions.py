"""Catálogo de permisos y los roles base de cada restaurante.

Un permiso es una acción que el servidor sabe comprobar: cada endpoint exige
uno. La lista es fija y vive en el código, porque cada permiso nuevo necesita un
endpoint que lo exija. Qué permisos tiene cada rol lo decide el restaurante:
los roles viven en la base (los posee `accounts`) y guardan códigos de este
catálogo.

Python puro: lo importan los casos de uso y el núcleo de autenticación.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


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
    ROLES_MANAGE = "roles.manage"


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
    Permission.ROLES_MANAGE: PermissionInfo("Crear roles y elegir sus permisos", _ADMINISTRACION),
}


class RoleKind(StrEnum):
    """Qué clase de rol es, no qué puede hacer.

    Todo restaurante nace con un `owner` (el encargado) y un `waiter` (el
    mesero), que no se borran. Los `custom` los crea el propio restaurante,
    como un cocinero o un cajero.
    """

    OWNER = "owner"
    WAITER = "waiter"
    CUSTOM = "custom"


# Con lo que nace el rol de mesero: ver el menú y las mesas, tomar pedidos y
# cobrarlos (el mesero hace de cajero). Qué pedidos ve y cuáles cobra (los que
# tomó) lo decide el caso de uso, no un permiso. Cada restaurante puede
# cambiarlo después.
DEFAULT_WAITER_PERMISSIONS = frozenset(
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


def effective_permissions(kind: RoleKind, stored: Iterable[str]) -> frozenset[Permission]:
    """Lo que un rol deja hacer de verdad.

    El encargado tiene el catálogo entero, calculado al leer y no guardado: así
    un permiso nuevo no queda huérfano por olvidarse de sumarlo a cada local.
    De lo guardado se descarta lo que ya no está en el catálogo.
    """
    if kind is RoleKind.OWNER:
        return frozenset(Permission)
    known = {permission.value for permission in Permission}
    return frozenset(Permission(code) for code in stored if code in known)


def ordered_catalog() -> list[Permission]:
    """El catálogo en el orden en que lo muestra la interfaz: por grupo."""
    group_order = {group: position for position, group in enumerate(PERMISSION_GROUPS)}
    catalog_order = {permission: position for position, permission in enumerate(CATALOG)}
    return sorted(
        CATALOG,
        key=lambda permission: (
            group_order[CATALOG[permission].group],
            catalog_order[permission],
        ),
    )
