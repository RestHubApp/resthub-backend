"""Reglas de dominio del menú, sin base ni servidor."""

from __future__ import annotations

from decimal import Decimal

import pytest

from resthub.modules.menu.domain.entities import MenuCategory, MenuItem, reorder
from resthub.modules.menu.domain.exceptions import (
    InvalidMenuCategory,
    InvalidMenuItem,
    InvalidOrdering,
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


def test_un_plato_nuevo_esta_en_la_carta_y_disponible() -> None:
    plato = _item(name="  Lomo   saltado ")

    assert plato.name == "Lomo saltado"
    assert plato.is_active and plato.is_available
    assert plato.can_be_ordered


@pytest.mark.parametrize("price", ["0", "-1", "12.345", "100000000.00"])
def test_un_precio_invalido_se_rechaza(price: str) -> None:
    with pytest.raises(InvalidMenuItem):
        _item(price=price)


def test_el_precio_se_guarda_con_centimos() -> None:
    assert str(_item(price="18").price) == "18.00"


def test_agotado_o_retirado_no_se_puede_pedir() -> None:
    agotado = _item()
    agotado.is_available = False
    retirado = _item()
    retirado.is_active = False

    assert not agotado.can_be_ordered
    assert not retirado.can_be_ordered


def test_la_categoria_no_puede_quedar_sin_nombre() -> None:
    with pytest.raises(InvalidMenuCategory):
        MenuCategory(restaurant_id=1, name="   ")


def test_reordenar_asigna_posiciones_en_el_orden_pedido() -> None:
    categorias = [
        MenuCategory(restaurant_id=1, name=nombre, id=identificador)
        for identificador, nombre in [(1, "Entradas"), (2, "Fondos"), (3, "Bebidas")]
    ]

    ordenadas = reorder(categorias, [3, 1, 2])

    assert [(c.id, c.position) for c in ordenadas] == [(3, 0), (1, 1), (2, 2)]


@pytest.mark.parametrize("ids", [[1, 2], [1, 2, 3, 99], [1, 1, 2]])
def test_reordenar_exige_nombrar_a_todos_exactamente_una_vez(ids: list[int]) -> None:
    categorias = [MenuCategory(restaurant_id=1, name=f"C{i}", id=i) for i in (1, 2, 3)]

    with pytest.raises(InvalidOrdering):
        reorder(categorias, ids)
