"""Los grupos de la bitácora: cada tipo tiene frase y cada grupo es coherente."""

from __future__ import annotations

import pytest

from resthub.core.activity import ActivityGroup, ActivityKind


@pytest.mark.parametrize("kind", list(ActivityKind))
def test_every_kind_has_a_label(kind: ActivityKind) -> None:
    assert kind.label


def test_sessions_group_holds_the_entry_and_the_exit() -> None:
    assert ActivityGroup.SESSIONS.kinds == {
        ActivityKind.SIGNED_IN,
        ActivityKind.SIGNED_OUT,
        ActivityKind.SESSION_CLOSED,
        ActivityKind.SESSION_LOCKED_OUT,
        ActivityKind.STAFF_SESSION_CLOSED,
        ActivityKind.RECOVERY_CODE_USED,
    }
    assert ActivityKind.SIGNED_OUT.label == "Cerró sesión"


def test_orders_group_includes_the_new_waiter_movements() -> None:
    assert {
        ActivityKind.ORDER_CREATED,
        ActivityKind.ORDER_ITEMS_ADDED,
        ActivityKind.ORDER_SERVED,
    } <= ActivityGroup.ORDERS.kinds


@pytest.mark.parametrize("group", list(ActivityGroup))
def test_every_group_has_kinds(group: ActivityGroup) -> None:
    assert group.kinds
