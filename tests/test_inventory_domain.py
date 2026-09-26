"""Reglas de dominio del inventario, sin base ni servidor."""

from __future__ import annotations

from decimal import Decimal

import pytest

from resthub.modules.inventory.domain.entities import (
    Ingredient,
    MovementKind,
    RecipeLine,
    StockLevel,
    StockMovement,
    Unit,
    recipe_cost,
    validate_recipe,
)
from resthub.modules.inventory.domain.exceptions import (
    InvalidIngredient,
    InvalidMovement,
    InvalidRecipe,
)
from resthub.modules.inventory.domain.purchasing import PurchaseOrderLine


def _arroz(**overrides: object) -> Ingredient:
    fields: dict[str, object] = {
        "restaurant_id": 1,
        "name": "Arroz",
        "unit": Unit.GRAM,
        "min_stock": Decimal("5000"),
        "unit_cost": Decimal("0.004"),
        "id": 1,
    }
    fields.update(overrides)
    return Ingredient(**fields)  # type: ignore[arg-type]


def _movement(kind: MovementKind, quantity: str, **extra: object) -> StockMovement:
    return StockMovement(
        restaurant_id=1,
        ingredient_id=1,
        kind=kind,
        quantity=Decimal(quantity),
        created_by=1,
        **extra,  # type: ignore[arg-type]
    )


def test_la_compra_pondera_el_costo_con_lo_que_habia() -> None:
    arroz = _arroz()

    # 5 kg a S/ 4 el kilo más 20 kg a S/ 5: el kilo queda en S/ 4.80.
    arroz.absorb_purchase(Decimal("5000"), Decimal("20000"), Decimal("0.005"))

    assert arroz.unit_cost == Decimal("0.004800")


@pytest.mark.parametrize("before", ["0", "-300"])
def test_sin_stock_previo_el_costo_es_el_de_la_compra(before: str) -> None:
    arroz = _arroz()

    arroz.absorb_purchase(Decimal(before), Decimal("1000"), Decimal("0.0055"))

    assert arroz.unit_cost == Decimal("0.005500")


def test_alerta_estrictamente_por_debajo_del_minimo() -> None:
    arroz = _arroz()

    assert StockLevel(arroz, Decimal("4999.999")).is_low
    assert not StockLevel(arroz, Decimal("5000")).is_low
    assert StockLevel(arroz, Decimal("-1")).is_negative


def test_el_minimo_no_puede_ser_negativo() -> None:
    with pytest.raises(InvalidIngredient):
        _arroz(min_stock=Decimal("-1"))


@pytest.mark.parametrize(
    ("kind", "quantity", "extra"),
    [
        (MovementKind.PURCHASE, "-1", {"unit_cost": Decimal("1")}),
        (MovementKind.PURCHASE, "10", {}),
        (MovementKind.WASTE, "10", {"reason": "se venció"}),
        (MovementKind.WASTE, "-10", {}),
        (MovementKind.ADJUSTMENT, "0", {"reason": "conteo"}),
        (MovementKind.ADJUSTMENT, "5", {}),
        (MovementKind.CONSUMPTION, "-5", {}),
        (MovementKind.CONSUMPTION, "5", {"order_id": 1, "order_item_id": 1}),
        (MovementKind.PURCHASE, "0.0001", {"unit_cost": Decimal("1")}),
    ],
)
def test_cada_tipo_de_movimiento_tiene_su_signo_y_sus_datos(
    kind: MovementKind, quantity: str, extra: dict[str, object]
) -> None:
    with pytest.raises(InvalidMovement):
        _movement(kind, quantity, **extra)


def test_movimientos_validos() -> None:
    compra = _movement(MovementKind.PURCHASE, "10", unit_cost=Decimal("0.5"))
    merma = _movement(MovementKind.WASTE, "-2", reason="  se   cayó ")
    ajuste = _movement(MovementKind.ADJUSTMENT, "-1.5", reason="conteo")
    consumo = _movement(MovementKind.CONSUMPTION, "-0.25", order_id=3, order_item_id=9)

    assert compra.quantity == Decimal("10.000")
    assert merma.reason == "se cayó"
    assert ajuste.quantity == Decimal("-1.500")
    assert consumo.order_item_id == 9


def test_la_receta_no_repite_insumos_y_pide_cantidades_positivas() -> None:
    with pytest.raises(InvalidRecipe):
        validate_recipe([RecipeLine(1, Decimal("10")), RecipeLine(1, Decimal("5"))])
    with pytest.raises(InvalidRecipe):
        RecipeLine(1, Decimal("0"))


def test_costo_de_receta() -> None:
    ingredientes = {
        1: _arroz(),
        2: _arroz(id=2, name="Pollo", unit_cost=Decimal("0.018")),
    }
    lineas = [RecipeLine(1, Decimal("150")), RecipeLine(2, Decimal("200"))]

    # 150 g x 0.004 + 200 g x 0.018 = 0.60 + 3.60
    assert recipe_cost(lineas, ingredientes) == Decimal("4.2")


def test_el_total_de_una_linea_de_compra_redondea_el_medio_centavo_hacia_arriba() -> None:
    linea = PurchaseOrderLine(
        ingredient_id=1, quantity=Decimal("1.000"), unit_cost=Decimal("1.005")
    )

    assert linea.estimated_total == Decimal("1.01")
