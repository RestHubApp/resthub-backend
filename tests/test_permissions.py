"""El catálogo de permisos y lo que deja hacer cada clase de rol."""

from __future__ import annotations

import pytest

from resthub.core.identity import MissingPermission, Principal, ensure_permission
from resthub.core.permissions import (
    CATALOG,
    DEFAULT_WAITER_PERMISSIONS,
    PERMISSION_GROUPS,
    Permission,
    RoleKind,
    effective_permissions,
    ordered_catalog,
)


def test_todo_permiso_tiene_etiqueta_y_grupo_conocido() -> None:
    assert set(CATALOG) == set(Permission)
    assert {info.group for info in CATALOG.values()} <= set(PERMISSION_GROUPS)


def test_el_encargado_tiene_todo_el_catalogo_aunque_no_este_guardado() -> None:
    assert effective_permissions(RoleKind.OWNER, ()) == frozenset(Permission)
    assert effective_permissions(RoleKind.OWNER, ["menu.read"]) == frozenset(Permission)


def test_de_lo_guardado_se_descarta_lo_que_no_esta_en_el_catalogo() -> None:
    assert effective_permissions(RoleKind.CUSTOM, ["orders.manage", "cocina.volar"]) == {
        Permission.ORDERS_MANAGE
    }


def test_el_mesero_nace_viendo_el_menu_y_las_mesas_tomando_pedidos_y_cobrando() -> None:
    assert DEFAULT_WAITER_PERMISSIONS == {
        Permission.MENU_READ,
        Permission.TABLES_READ,
        Permission.ORDERS_TAKE,
        # El mesero hace de cajero: cobra y emite el comprobante.
        Permission.ORDERS_CHARGE,
        Permission.BILLING_ISSUE,
        Permission.CUSTOMERS_READ,
        Permission.CUSTOMERS_MANAGE,
        Permission.RESERVATIONS_READ,
        Permission.RESERVATIONS_MANAGE,
    }


def test_el_catalogo_se_ordena_por_grupo() -> None:
    orden = ordered_catalog()
    grupos = [CATALOG[permission].group for permission in orden]

    assert set(orden) == set(Permission)
    assert sorted(grupos, key=PERMISSION_GROUPS.index) == grupos
    assert orden[-1] is Permission.ROLES_MANAGE


def test_alcanza_con_uno_de_los_permisos_pedidos() -> None:
    mesero = Principal(
        user_id=1,
        role_id=2,
        is_active=True,
        restaurant_id=1,
        permissions=frozenset(p.value for p in DEFAULT_WAITER_PERMISSIONS),
    )

    ensure_permission(mesero, Permission.STAFF_MANAGE, Permission.ORDERS_TAKE)
    with pytest.raises(MissingPermission):
        ensure_permission(mesero, Permission.STAFF_MANAGE)
