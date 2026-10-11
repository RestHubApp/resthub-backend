"""Reglas de dominio del menú, sin base ni servidor."""

from __future__ import annotations

from decimal import Decimal

import pytest

from resthub.modules.menu.domain.entities import MenuCategory, MenuItem, reorder
from resthub.modules.menu.domain.exceptions import (
    InvalidMenuCategoryError,
    InvalidMenuItemError,
    InvalidOrderingError,
)


def _item(price: str = "12.50", **overrides: object) -> MenuItem:
    fields: dict[str, object] = {
        "restaurant_id": 1,
        "category_id": 1,
        "name": "Ceviche clásico",
        "price": Decimal(price),
    }
    fields.update(overrides)
    return MenuItem(**fields)  # type: ignore[arg-type]


def test_new_dish_is_on_the_menu_and_available() -> None:
    dish = _item(name="  Lomo   saltado ")

    assert dish.name == "Lomo saltado"
    assert dish.is_active and dish.is_available
    assert dish.can_be_ordered


@pytest.mark.parametrize("price", ["0", "-1", "12.345", "100000000.00"])
def test_invalid_price_is_rejected(price: str) -> None:
    with pytest.raises(InvalidMenuItemError):
        _item(price=price)


def test_price_is_stored_with_cents() -> None:
    assert str(_item(price="18").price) == "18.00"


def test_sold_out_or_withdrawn_cannot_be_ordered() -> None:
    sold_out = _item()
    sold_out.is_available = False
    withdrawn = _item()
    withdrawn.is_active = False

    assert not sold_out.can_be_ordered
    assert not withdrawn.can_be_ordered


def test_category_cannot_be_left_unnamed() -> None:
    with pytest.raises(InvalidMenuCategoryError):
        MenuCategory(restaurant_id=1, name="   ")


def test_reorder_assigns_positions_in_requested_order() -> None:
    categories = [
        MenuCategory(restaurant_id=1, name=name, id=identifier)
        for identifier, name in [(1, "Entradas"), (2, "Fondos"), (3, "Bebidas")]
    ]

    ordered = reorder(categories, [3, 1, 2])

    assert [(c.id, c.position) for c in ordered] == [(3, 0), (1, 1), (2, 2)]


@pytest.mark.parametrize("ids", [[1, 2], [1, 2, 3, 99], [1, 1, 2]])
def test_reorder_requires_naming_every_item_exactly_once(ids: list[int]) -> None:
    categories = [MenuCategory(restaurant_id=1, name=f"C{i}", id=i) for i in (1, 2, 3)]

    with pytest.raises(InvalidOrderingError):
        reorder(categories, ids)
