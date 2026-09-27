"""Límites y mensajes del dominio del inventario: insumos, movimientos, recetas y compras.

Complementa `test_inventory_domain.py`: acá se fija el primer valor que se
rechaza y el último que se acepta, el mensaje exacto y las cuentas del costo
ponderado y de las sugerencias de compra con sus redondeos.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.modules.inventory.domain.entities import (
    MAX_NAME_LENGTH,
    MAX_QUANTITY,
    MAX_REASON_LENGTH,
    MAX_UNIT_COST,
    NO_STOCK,
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
    InvalidPurchaseOrder,
    InvalidRecipe,
    InvalidSupplier,
)
from resthub.modules.inventory.domain.purchasing import (
    MAX_LINES,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Receipt,
    Supplier,
    suggest_purchase,
)

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
D = Decimal


def _insumo(**datos: object) -> Ingredient:
    campos: dict[str, object] = {"restaurant_id": 1, "name": "Arroz", "unit": Unit.GRAM, "id": 1}
    campos.update(datos)
    return Ingredient(**campos)  # type: ignore[arg-type]


def _movimiento(kind: MovementKind, cantidad: str, **extra: object) -> StockMovement:
    return StockMovement(1, 1, kind, D(cantidad), created_by=1, **extra)  # type: ignore[arg-type]


def _linea(insumo: int = 1, cantidad: str = "10", costo: str = "2.5", **extra: object):
    return PurchaseOrderLine(insumo, D(cantidad), D(costo), **extra)  # type: ignore[arg-type]


def _orden(*lineas: PurchaseOrderLine, **extra: object) -> PurchaseOrder:
    return PurchaseOrder(1, 7, 3, 1, list(lineas or (_linea(id=1),)), **extra)  # type: ignore[arg-type]


# -- Unidades y tipos -----------------------------------------------------------


def test_unidades_y_tipos_de_movimiento_tienen_su_rotulo() -> None:
    assert [u.label for u in Unit] == ["gramos", "mililitros", "unidades"]
    assert [k.label for k in MovementKind] == ["Compra", "Consumo", "Merma", "Ajuste"]
    assert str(NO_STOCK) == "0.000"


# -- Insumos --------------------------------------------------------------------


def test_un_insumo_nuevo_arranca_sin_minimo_ni_costo_y_activo() -> None:
    insumo = _insumo(name="  Arroz   extra ")

    assert insumo.name == "Arroz extra"
    assert (insumo.min_stock, insumo.unit_cost, insumo.is_active) == (D("0"), D("0"), True)
    assert str(insumo.min_stock) == "0.000"
    assert str(insumo.unit_cost) == "0.000000"


def test_renombrar_valida_vacio_y_largo_maximo() -> None:
    insumo = _insumo()

    insumo.rename("A" * MAX_NAME_LENGTH)
    assert len(insumo.name) == MAX_NAME_LENGTH
    with pytest.raises(InvalidIngredient) as largo:
        insumo.rename("A" * (MAX_NAME_LENGTH + 1))
    with pytest.raises(InvalidIngredient) as vacio:
        insumo.rename("  ")
    assert largo.value.reason == "El nombre del insumo no puede pasar de 80."
    assert vacio.value.reason == "El nombre del insumo no puede quedar vacío."


def test_el_minimo_y_el_costo_se_cambian_con_su_escala() -> None:
    insumo = _insumo()

    insumo.set_min_stock(D("2.5"))
    insumo.set_unit_cost(D("0.0042"))

    assert str(insumo.min_stock) == "2.500"
    assert str(insumo.unit_cost) == "0.004200"
    insumo.set_min_stock(D("0"))
    assert insumo.min_stock == 0


@pytest.mark.parametrize("costo", ["-0.000001", "NaN", "Infinity", "100000000"])
def test_un_costo_unitario_fuera_de_rango_se_rechaza(costo: str) -> None:
    with pytest.raises(InvalidIngredient) as error:
        _insumo().set_unit_cost(D(costo))
    assert error.value.reason == "El costo unitario tiene que ser un monto positivo razonable."


def test_el_costo_unitario_acepta_cero_y_el_tope() -> None:
    insumo = _insumo()

    insumo.set_unit_cost(MAX_UNIT_COST)
    assert insumo.unit_cost == MAX_UNIT_COST
    insumo.set_unit_cost(D("0"))
    assert insumo.unit_cost == 0


@pytest.mark.parametrize(
    ("cantidad", "mensaje"),
    [
        ("NaN", "La cantidad no es válida."),
        ("1000000000", "La cantidad no es válida."),
        ("-1000000000", "La cantidad no es válida."),
        ("0.0005", "Las cantidades admiten como máximo tres decimales."),
        ("-0.001", "El stock mínimo no puede ser negativo."),
    ],
)
def test_un_minimo_invalido_explica_el_motivo(cantidad: str, mensaje: str) -> None:
    with pytest.raises(InvalidIngredient) as error:
        _insumo().set_min_stock(D(cantidad))
    assert error.value.reason == mensaje


def test_el_minimo_admite_la_cantidad_maxima() -> None:
    assert _insumo(min_stock=MAX_QUANTITY).min_stock == MAX_QUANTITY


def test_el_costo_ponderado_mezcla_lo_viejo_con_lo_nuevo() -> None:
    # 5 kg a S/ 4 y llegan 20 kg a S/ 5: (5*4 + 20*5) / 25 = 4.8.
    insumo = _insumo(unit_cost=D("4"))

    insumo.absorb_purchase(D("5"), D("20"), D("5"))

    assert insumo.unit_cost == D("4.800000")


def test_el_costo_ponderado_conserva_seis_decimales() -> None:
    insumo = _insumo(unit_cost=D("0.004"))

    insumo.absorb_purchase(D("1000"), D("2000"), D("0.0045"))

    # (1000*0.004 + 2000*0.0045) / 3000 = 13 / 3000 = 0.00433333…
    assert insumo.unit_cost == D("0.004333")


def test_con_stock_negativo_el_costo_es_el_de_la_compra() -> None:
    insumo = _insumo(unit_cost=D("9"))

    insumo.absorb_purchase(D("-3"), D("10"), D("2"))

    assert insumo.unit_cost == D("2.000000")


def test_un_costo_de_compra_invalido_no_cambia_el_insumo() -> None:
    insumo = _insumo(unit_cost=D("4"))

    with pytest.raises(InvalidIngredient):
        insumo.absorb_purchase(D("0"), D("1"), D("-1"))
    assert insumo.unit_cost == D("4")


# -- Movimientos ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "cantidad", "extra", "mensaje"),
    [
        (
            MovementKind.PURCHASE,
            "0",
            {"unit_cost": D("1")},
            "Una compra tiene que sumar una cantidad positiva.",
        ),
        (MovementKind.PURCHASE, "1", {}, "Una compra necesita su costo unitario."),
        (
            MovementKind.CONSUMPTION,
            "0",
            {"order_id": 1, "order_item_id": 1},
            "Un consumo tiene que restar.",
        ),
        (
            MovementKind.CONSUMPTION,
            "-1",
            {"order_id": 1},
            "Un consumo sale siempre de un plato servido.",
        ),
        (
            MovementKind.CONSUMPTION,
            "-1",
            {"order_item_id": 1},
            "Un consumo sale siempre de un plato servido.",
        ),
        (MovementKind.WASTE, "0", {"reason": "se venció"}, "Una merma tiene que restar."),
        (MovementKind.WASTE, "-1", {"reason": "   "}, "Una merma exige indicar el motivo."),
        (MovementKind.ADJUSTMENT, "0", {"reason": "conteo"}, "Un ajuste de cero no cambia nada."),
        (MovementKind.ADJUSTMENT, "-1", {}, "Un ajuste exige indicar el motivo."),
        (
            MovementKind.ADJUSTMENT,
            "1",
            {"reason": "x" * (MAX_REASON_LENGTH + 1)},
            "El motivo admite 200 caracteres.",
        ),
        (
            MovementKind.PURCHASE,
            "1",
            {"unit_cost": D("-1")},
            "El costo unitario tiene que ser un monto positivo razonable.",
        ),
        (MovementKind.ADJUSTMENT, "Infinity", {"reason": "conteo"}, "La cantidad no es válida."),
    ],
)
def test_cada_movimiento_invalido_explica_el_motivo(
    kind: MovementKind, cantidad: str, extra: dict[str, object], mensaje: str
) -> None:
    with pytest.raises(InvalidMovement) as error:
        _movimiento(kind, cantidad, **extra)
    assert error.value.reason == mensaje


def test_los_movimientos_limite_se_aceptan() -> None:
    compra = _movimiento(MovementKind.PURCHASE, "0.001", unit_cost=D("0"))
    ajuste = _movimiento(MovementKind.ADJUSTMENT, "0.001", reason="x" * MAX_REASON_LENGTH)
    merma = _movimiento(MovementKind.WASTE, "-0.001", reason="se cayó")

    assert compra.quantity == D("0.001")
    assert compra.unit_cost == D("0.000000")
    assert len(ajuste.reason) == MAX_REASON_LENGTH
    assert merma.quantity == D("-0.001")


def test_un_consumo_guarda_el_costo_vigente_con_seis_decimales() -> None:
    consumo = _movimiento(
        MovementKind.CONSUMPTION, "-150", order_id=3, order_item_id=9, unit_cost=D("0.0042")
    )

    assert str(consumo.unit_cost) == "0.004200"
    assert consumo.reason == ""


# -- Stock y recetas -------------------------------------------------------------------


def test_stock_bajo_y_negativo() -> None:
    insumo = _insumo(min_stock=D("10"))

    assert StockLevel(insumo, D("9.999")).is_low
    assert not StockLevel(insumo, D("10")).is_low
    assert not StockLevel(insumo, D("0")).is_negative
    assert StockLevel(insumo, D("-0.001")).is_negative


def test_la_receta_guarda_tres_decimales_y_rechaza_negativos() -> None:
    assert str(RecipeLine(1, D("150.5")).quantity) == "150.500"

    with pytest.raises(InvalidRecipe) as negativa:
        RecipeLine(1, D("-1"))
    with pytest.raises(InvalidRecipe) as decimales:
        RecipeLine(1, D("0.0001"))
    assert negativa.value.reason == "Cada insumo de la receta necesita una cantidad positiva."
    assert decimales.value.reason == "Las cantidades admiten como máximo tres decimales."


def test_una_receta_valida_se_devuelve_en_su_orden() -> None:
    lineas = [RecipeLine(2, D("1")), RecipeLine(1, D("2"))]

    assert validate_recipe(iter(lineas)) == lineas
    assert validate_recipe([]) == []
    with pytest.raises(InvalidRecipe) as error:
        validate_recipe([RecipeLine(1, D("1")), RecipeLine(1, D("2"))])
    assert error.value.reason == "Un insumo aparece dos veces en la receta; suma las cantidades."


def test_el_costo_de_receta_salta_insumos_desconocidos_y_no_redondea() -> None:
    insumos = {1: _insumo(unit_cost=D("0.0042"))}

    costo = recipe_cost([RecipeLine(1, D("150")), RecipeLine(99, D("10"))], insumos)

    assert costo == D("0.63")
    assert recipe_cost([], insumos) == 0


# -- Proveedores ---------------------------------------------------------------------


def test_un_proveedor_limpia_sus_textos() -> None:
    proveedor = Supplier(1, "  Mercado   Central ", contact=" Rosa ", phone=" 01 555 ", notes=" x ")

    assert (proveedor.name, proveedor.contact, proveedor.phone, proveedor.notes) == (
        "Mercado Central",
        "Rosa",
        "01 555",
        "x",
    )
    assert proveedor.is_active


@pytest.mark.parametrize(
    ("datos", "mensaje"),
    [
        ({"name": "  "}, "El proveedor necesita un nombre."),
        ({"name": "N" * 81}, "El nombre del proveedor admite 80 caracteres como máximo."),
        ({"contact": "C" * 81}, "El contacto admite 80 caracteres como máximo."),
        ({"phone": "9" * 21}, "El teléfono admite 20 caracteres como máximo."),
        ({"notes": "N" * 301}, "La nota admite 300 caracteres como máximo."),
    ],
)
def test_un_proveedor_invalido_explica_el_motivo(datos: dict[str, str], mensaje: str) -> None:
    campos = {"restaurant_id": 1, "name": "Makro", **datos}

    with pytest.raises(InvalidSupplier) as error:
        Supplier(**campos)  # type: ignore[arg-type]
    assert error.value.reason == mensaje


def test_un_proveedor_admite_los_largos_maximos() -> None:
    proveedor = Supplier(1, "N" * 80, contact="C" * 80, phone="9" * 20, notes="N" * 300)

    assert (len(proveedor.name), len(proveedor.phone), len(proveedor.notes)) == (80, 20, 300)


# -- Órdenes de compra ----------------------------------------------------------------


def test_las_etiquetas_de_estado_de_una_orden() -> None:
    assert [s.label for s in PurchaseOrderStatus] == [
        "Borrador",
        "Enviada",
        "Recibida",
        "Cancelada",
    ]


def test_una_linea_normaliza_cantidad_y_costo() -> None:
    linea = _linea(cantidad="2.5", costo="0.0042")

    assert str(linea.quantity) == "2.500"
    assert str(linea.unit_cost) == "0.004200"
    assert linea.received_total == D("0.00")


@pytest.mark.parametrize(
    ("cantidad", "costo", "mensaje"),
    [
        ("0", "1", "La cantidad tiene que ser mayor que cero."),
        ("-1", "1", "La cantidad tiene que ser mayor que cero."),
        ("NaN", "1", "La cantidad tiene que ser mayor que cero."),
        ("1", "-0.01", "El costo no puede ser negativo."),
        ("1", "Infinity", "El costo no puede ser negativo."),
    ],
)
def test_una_linea_invalida_explica_el_motivo(cantidad: str, costo: str, mensaje: str) -> None:
    with pytest.raises(InvalidPurchaseOrder) as error:
        _linea(cantidad=cantidad, costo=costo)
    assert error.value.reason == mensaje


def test_una_linea_de_costo_cero_es_valida() -> None:
    assert _linea(costo="0").estimated_total == D("0.00")


def test_la_orden_suma_lo_estimado_y_lo_recibido() -> None:
    orden = _orden(_linea(1, "10", "2.5", id=1), _linea(2, "3", "1.335", id=2))

    assert orden.estimated_total == D("29.01")
    assert orden.received_total == D("0.00")


@pytest.mark.parametrize(
    ("lineas", "mensaje"),
    [
        ([], "La orden necesita al menos un insumo."),
        ([_linea(i) for i in range(MAX_LINES + 1)], "Una orden admite hasta 60 insumos."),
        ([_linea(1), _linea(1)], "Un insumo aparece dos veces en la orden."),
    ],
)
def test_una_orden_mal_armada_se_rechaza(lineas: list[PurchaseOrderLine], mensaje: str) -> None:
    with pytest.raises(InvalidPurchaseOrder) as error:
        PurchaseOrder(1, 7, 3, 1, lineas)
    assert error.value.reason == mensaje


def test_una_orden_admite_sesenta_insumos_y_una_nota_de_trescientos() -> None:
    orden = PurchaseOrder(1, 7, 3, 1, [_linea(i) for i in range(MAX_LINES)], notes="N" * 300)

    assert len(orden.lines) == MAX_LINES
    with pytest.raises(InvalidSupplier):
        PurchaseOrder(1, 7, 3, 1, [_linea()], notes="N" * 301)


def test_el_borrador_se_edita_y_la_nota_se_conserva_si_no_llega() -> None:
    orden = _orden(notes="Urgente")

    orden.replace_lines([_linea(5, "1", "1")], None)
    assert [line.ingredient_id for line in orden.lines] == [5]
    assert orden.notes == "Urgente"
    orden.replace_lines([_linea(6, "1", "1")], "  Para  el lunes ")
    assert orden.notes == "Para el lunes"


def test_una_orden_enviada_ya_no_se_edita_ni_se_reenvia() -> None:
    orden = _orden()
    orden.send(NOW)

    assert (orden.status, orden.sent_at) == (PurchaseOrderStatus.SENT, NOW)
    with pytest.raises(InvalidPurchaseOrder) as editar:
        orden.replace_lines([_linea(9)], None)
    with pytest.raises(InvalidPurchaseOrder) as enviar:
        orden.send(NOW)
    assert editar.value.reason == "Una orden «Enviada» no se puede editar."
    assert enviar.value.reason == "Una orden «Enviada» no se puede enviar."


def test_recibir_anota_lo_que_llego_y_devuelve_solo_lo_que_entra() -> None:
    orden = _orden(
        _linea(1, "10", "2", id=11), _linea(2, "5", "3", id=12), _linea(3, "1", "4", id=13)
    )

    entran = orden.receive(
        [Receipt(11, D("12.5"), D("1.9")), Receipt(13, D("0"), D("4"))],
        NOW,
    )

    assert [line.id for line in entran] == [11]
    uno, dos, tres = orden.lines
    assert (uno.received_quantity, uno.received_unit_cost) == (D("12.500"), D("1.900000"))
    # Lo que no llegó queda en cero, con el costo estimado.
    assert (dos.received_quantity, dos.received_unit_cost) == (D("0.000"), D("3.000000"))
    assert tres.received_quantity == D("0.000")
    assert orden.received_total == D("23.75")
    assert (orden.status, orden.received_at) == (PurchaseOrderStatus.RECEIVED, NOW)


def test_se_recibe_tambien_un_borrador_sin_enviar() -> None:
    orden = _orden(_linea(1, "2", "1", id=1))

    entran = orden.receive([Receipt(1, D("2"), D("1"))], NOW)

    assert [line.id for line in entran] == [1]
    assert orden.status is PurchaseOrderStatus.RECEIVED
    assert orden.sent_at is None


def test_si_no_llego_nada_no_entra_nada_al_stock() -> None:
    orden = _orden(_linea(1, "2", "1", id=1))

    assert orden.receive([], NOW) == []
    assert orden.lines[0].received_quantity == D("0.000")
    assert orden.status is PurchaseOrderStatus.RECEIVED


@pytest.mark.parametrize(
    ("recibo", "mensaje"),
    [
        (Receipt(99, D("1"), D("1")), "La orden no tiene la línea 99."),
        (Receipt(1, D("-0.001"), D("1")), "Lo recibido no puede ser negativo."),
        (Receipt(1, D("NaN"), D("1")), "Lo recibido no puede ser negativo."),
        (Receipt(1, D("1"), D("-1")), "El costo no puede ser negativo."),
    ],
)
def test_un_recibo_invalido_se_rechaza(recibo: Receipt, mensaje: str) -> None:
    orden = _orden(_linea(1, "2", "1", id=1))

    with pytest.raises(InvalidPurchaseOrder) as error:
        orden.receive([recibo], NOW)
    assert error.value.reason == mensaje


@pytest.mark.parametrize("cierre", ["receive", "cancel"])
def test_una_orden_cerrada_no_se_recibe_ni_se_cancela(cierre: str) -> None:
    orden = _orden()
    if cierre == "receive":
        orden.receive([], NOW)
    else:
        orden.cancel(NOW)
    rotulo = orden.status.label

    with pytest.raises(InvalidPurchaseOrder) as recibir:
        orden.receive([], NOW)
    with pytest.raises(InvalidPurchaseOrder) as cancelar:
        orden.cancel(NOW)
    assert recibir.value.reason == f"Una orden «{rotulo}» ya no se recibe."
    assert cancelar.value.reason == f"Una orden «{rotulo}» ya no se cancela."


def test_cancelar_una_orden_enviada_anota_cuando() -> None:
    orden = _orden()
    orden.send(NOW)

    orden.cancel(NOW)

    assert (orden.status, orden.cancelled_at) == (PurchaseOrderStatus.CANCELLED, NOW)


# -- Sugerencias de compra ---------------------------------------------------------------


def test_bien_cubierto_no_se_sugiere() -> None:
    # Justo en el mínimo y con tres días de consumo exactos: todavía alcanza.
    assert suggest_purchase(1, D("30"), D("30"), D("10"), D("1")) is None
    assert suggest_purchase(1, D("5"), D("0"), D("0"), D("1")) is None


def test_bajo_el_minimo_se_pide_el_doble_del_minimo() -> None:
    sugerencia = suggest_purchase(1, D("4"), D("10"), D("0"), D("0.5"))

    assert sugerencia is not None
    assert sugerencia.quantity == D("16.000")
    assert (sugerencia.stock, sugerencia.min_stock, sugerencia.unit_cost) == (
        D("4"),
        D("10"),
        D("0.5"),
    )


def test_con_poca_cobertura_se_pide_una_semana_de_consumo() -> None:
    # Hay 20 y se usan 7.5 por día: menos de tres días. Una semana son 52.5.
    sugerencia = suggest_purchase(2, D("20"), D("5"), D("7.5"), D("1"))

    assert sugerencia is not None
    assert sugerencia.ingredient_id == 2
    assert sugerencia.quantity == D("32.500")
    assert sugerencia.average_daily_use == D("7.500")


def test_con_stock_negativo_se_pide_el_objetivo_completo() -> None:
    sugerencia = suggest_purchase(1, D("-6"), D("10"), D("1"), D("1"))

    assert sugerencia is not None
    assert sugerencia.quantity == D("20.000")


def test_una_cantidad_que_redondea_a_cero_no_se_sugiere() -> None:
    # Bajo un mínimo de 0.0001, el objetivo (0.0002) redondea a 0.000: no hay
    # nada que pedir aunque el insumo figure por debajo del mínimo.
    assert suggest_purchase(1, D("0"), D("0.0001"), D("0"), D("1")) is None
    sugerencia = suggest_purchase(1, D("20"), D("25"), D("0"), D("1"))
    assert sugerencia is not None
    assert sugerencia.quantity == D("30.000")
