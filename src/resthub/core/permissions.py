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
    # Derechos ARCO (Ley N.º 29733): exportar todo lo de un cliente y borrarlo.
    # Borrar no se deshace, así que el mesero no lo trae por omisión.
    CUSTOMERS_ERASE = "customers.erase"
    RESERVATIONS_READ = "reservations.read"
    RESERVATIONS_MANAGE = "reservations.manage"
    INVENTORY_READ = "inventory.read"
    INVENTORY_MANAGE = "inventory.manage"
    # Una merma que vale más que el monto del local no toca el stock hasta que
    # alguien con este permiso la aprueba.
    INVENTORY_APPROVE_WASTE = "inventory.approve_waste"
    STAFF_MANAGE = "staff.manage"
    RESTAURANT_MANAGE = "restaurant.manage"
    INSIGHTS_READ = "insights.read"
    ACTIVITY_READ = "activity.read"
    ROLES_MANAGE = "roles.manage"


class PlatformPermission(StrEnum):
    """Permisos de la administración del sistema.

    Catálogo aparte de `Permission` a propósito: el encargado de un restaurante
    tiene `frozenset(Permission)` y nunca debe recibir estos, y el editor de
    roles del restaurante tampoco los ofrece. Una credencial de restaurante no
    los tiene ni los puede pedir.
    """

    RESTAURANTS_READ = "platform.restaurants.read"
    RESTAURANTS_MANAGE = "platform.restaurants.manage"
    ACTIVITY_READ = "platform.activity.read"
    PREVIEW_OPEN = "platform.preview.open"
    OBSERVABILITY_READ = "platform.observability.read"


@dataclass(frozen=True, slots=True)
class PermissionInfo:
    label: str
    group: str


_MENU = "Menú"
_TABLES = "Mesas"
_ORDERS = "Pedidos"
_CASH = "Caja"
_INVENTORY = "Inventario"
_CUSTOMERS = "Clientes y reservas"
_ADMINISTRATION = "Administración"

# El orden en que la interfaz muestra los grupos.
PERMISSION_GROUPS: tuple[str, ...] = (
    _MENU,
    _TABLES,
    _ORDERS,
    _CASH,
    _CUSTOMERS,
    _INVENTORY,
    _ADMINISTRATION,
)

CATALOG: dict[Permission, PermissionInfo] = {
    Permission.MENU_READ: PermissionInfo("Ver el menú", _MENU),
    Permission.MENU_MANAGE: PermissionInfo("Editar categorías, platos y precios", _MENU),
    Permission.TABLES_READ: PermissionInfo("Ver las mesas", _TABLES),
    Permission.TABLES_MANAGE: PermissionInfo("Agregar y editar mesas", _TABLES),
    Permission.ORDERS_TAKE: PermissionInfo(
        "Tomar pedidos, agregar platos y marcarlos servidos", _ORDERS
    ),
    Permission.ORDERS_READ_ALL: PermissionInfo("Ver todos los pedidos del local", _ORDERS),
    Permission.ORDERS_MANAGE: PermissionInfo(
        "Cambiar el estado en cocina y cancelar pedidos", _ORDERS
    ),
    Permission.ORDERS_CHARGE: PermissionInfo(
        "Cobrar pedidos y dar descuentos hasta el tope del mesero", _CASH
    ),
    Permission.ORDERS_DISCOUNT_ANY: PermissionInfo(
        "Dar descuentos sin tope e invitar platos (cortesías)", _CASH
    ),
    Permission.CASH_MANAGE: PermissionInfo("Abrir y cerrar la caja y ver sus arqueos", _CASH),
    Permission.BILLING_ISSUE: PermissionInfo("Emitir boletas y facturas de lo cobrado", _CASH),
    Permission.BILLING_MANAGE: PermissionInfo(
        "Configurar los datos fiscales y revisar los comprobantes", _CASH
    ),
    Permission.CUSTOMERS_READ: PermissionInfo("Buscar clientes y ver su ficha", _CUSTOMERS),
    Permission.CUSTOMERS_MANAGE: PermissionInfo("Dar de alta y editar clientes", _CUSTOMERS),
    Permission.CUSTOMERS_ERASE: PermissionInfo(
        "Exportar y borrar los datos de un cliente (derechos ARCO)", _CUSTOMERS
    ),
    Permission.RESERVATIONS_READ: PermissionInfo("Ver las reservas del día", _CUSTOMERS),
    Permission.RESERVATIONS_MANAGE: PermissionInfo("Tomar, editar y cerrar reservas", _CUSTOMERS),
    Permission.INVENTORY_READ: PermissionInfo("Ver insumos y stock", _INVENTORY),
    Permission.INVENTORY_MANAGE: PermissionInfo(
        "Registrar compras, mermas, ajustes, conteos físicos y recetas", _INVENTORY
    ),
    Permission.INVENTORY_APPROVE_WASTE: PermissionInfo(
        "Aprobar o rechazar las mermas grandes", _INVENTORY
    ),
    Permission.STAFF_MANAGE: PermissionInfo(
        "Dar de alta y administrar al personal", _ADMINISTRATION
    ),
    Permission.RESTAURANT_MANAGE: PermissionInfo(
        "Editar los datos del restaurante", _ADMINISTRATION
    ),
    Permission.INSIGHTS_READ: PermissionInfo("Ver indicadores y predicciones", _ADMINISTRATION),
    Permission.ACTIVITY_READ: PermissionInfo("Ver la bitácora de movimientos", _ADMINISTRATION),
    Permission.ROLES_MANAGE: PermissionInfo("Crear roles y elegir sus permisos", _ADMINISTRATION),
}


_SYSTEM_ADMINISTRATION = "Administración del sistema"

PLATFORM_CATALOG: dict[PlatformPermission, PermissionInfo] = {
    PlatformPermission.RESTAURANTS_READ: PermissionInfo(
        "Ver los restaurantes y su ficha", _SYSTEM_ADMINISTRATION
    ),
    PlatformPermission.RESTAURANTS_MANAGE: PermissionInfo(
        "Dar de alta, editar, activar y desactivar restaurantes y agregar encargados",
        _SYSTEM_ADMINISTRATION,
    ),
    PlatformPermission.ACTIVITY_READ: PermissionInfo(
        "Ver la bitácora de la plataforma", _SYSTEM_ADMINISTRATION
    ),
    PlatformPermission.PREVIEW_OPEN: PermissionInfo(
        "Abrir la vista previa («Ver como…») y reiniciar el local de muestra",
        _SYSTEM_ADMINISTRATION,
    ),
    PlatformPermission.OBSERVABILITY_READ: PermissionInfo(
        "Ver el panel de observabilidad", _SYSTEM_ADMINISTRATION
    ),
}


def platform_permissions() -> frozenset[PlatformPermission]:
    """Lo que puede hacer una cuenta de la administración del sistema.

    Hoy toda cuenta de plataforma tiene el catálogo entero, calculado al leer y
    no guardado (como el encargado): no hay columna ni migración. Este es el
    punto de extensión para roles de plataforma futuros: cuando existan, esta
    función recibirá la cuenta y devolverá lo que su rol deja hacer.
    """
    return frozenset(PlatformPermission)


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
