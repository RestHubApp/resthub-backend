"""El mapa fijo de rol a permisos."""

from __future__ import annotations

import pytest

from resthub.core.identity import MissingPermission, Principal, Role, ensure_permission
from resthub.core.permissions import CATALOG, PERMISSION_GROUPS, Permission, permissions_for


def test_todo_permiso_tiene_etiqueta_y_grupo_conocido() -> None:
    assert set(CATALOG) == set(Permission)
    assert {info.group for info in CATALOG.values()} <= set(PERMISSION_GROUPS)


def test_el_encargado_tiene_todos_los_permisos() -> None:
    assert permissions_for(Role.ADMIN) == frozenset(Permission)


def test_el_mesero_solo_ve_el_menu_y_las_mesas_y_toma_pedidos() -> None:
    assert permissions_for(Role.WAITER) == {
        Permission.MENU_READ,
        Permission.TABLES_READ,
        Permission.ORDERS_TAKE,
    }


def test_las_etiquetas_de_los_roles() -> None:
    assert Role.ADMIN.label == "Encargado"
    assert Role.WAITER.label == "Mesero"


def test_alcanza_con_uno_de_los_permisos_pedidos() -> None:
    mesero = Principal(
        user_id=1,
        role=Role.WAITER,
        is_active=True,
        restaurant_id=1,
        permissions=frozenset(p.value for p in permissions_for(Role.WAITER)),
    )

    ensure_permission(mesero, Permission.STAFF_MANAGE, Permission.ORDERS_TAKE)
    with pytest.raises(MissingPermission):
        ensure_permission(mesero, Permission.STAFF_MANAGE)
