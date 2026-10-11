"""El catálogo de permisos y lo que deja hacer cada clase de rol."""

from __future__ import annotations

import pytest

from resthub.core.identity import MissingPermissionError, Principal, ensure_permission
from resthub.core.permissions import (
    CATALOG,
    DEFAULT_WAITER_PERMISSIONS,
    PERMISSION_GROUPS,
    Permission,
    RoleKind,
    effective_permissions,
    ordered_catalog,
)


def test_every_permission_has_label_and_known_group() -> None:
    assert set(CATALOG) == set(Permission)
    assert {info.group for info in CATALOG.values()} <= set(PERMISSION_GROUPS)


def test_owner_has_whole_catalog_even_if_not_stored() -> None:
    assert effective_permissions(RoleKind.OWNER, ()) == frozenset(Permission)
    assert effective_permissions(RoleKind.OWNER, [Permission.MENU_READ.value]) == frozenset(
        Permission
    )


def test_stored_permissions_not_in_catalog_are_discarded() -> None:
    assert effective_permissions(
        RoleKind.CUSTOM, [Permission.ORDERS_MANAGE.value, "cocina.volar"]
    ) == {Permission.ORDERS_MANAGE}


def test_waiter_starts_with_menu_tables_taking_orders_and_charging() -> None:
    assert {
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
    } == DEFAULT_WAITER_PERMISSIONS


def test_catalog_is_ordered_by_group() -> None:
    ordered = ordered_catalog()
    groups = [CATALOG[permission].group for permission in ordered]

    assert set(ordered) == set(Permission)
    assert sorted(groups, key=PERMISSION_GROUPS.index) == groups
    assert ordered[-1] is Permission.ROLES_MANAGE


def test_one_of_the_requested_permissions_is_enough() -> None:
    waiter = Principal(
        user_id=1,
        role_id=2,
        is_active=True,
        restaurant_id=1,
        permissions=frozenset(p.value for p in DEFAULT_WAITER_PERMISSIONS),
    )

    ensure_permission(waiter, Permission.STAFF_MANAGE, Permission.ORDERS_TAKE)
    with pytest.raises(MissingPermissionError):
        ensure_permission(waiter, Permission.STAFF_MANAGE)
