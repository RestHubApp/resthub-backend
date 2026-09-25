"""Cuentas de los indicadores y reglas fijas, con datos armados a mano."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from resthub.modules.insights.domain.decisions import (
    Engine,
    NoteType,
    RestockAction,
    Urgency,
    Verdict,
    WasteCause,
)
from resthub.modules.insights.domain.exceptions import InvalidPeriod
from resthub.modules.insights.domain.explanations import amount, explain_restock
from resthub.modules.insights.domain.period import DateRange, resolve_range
from resthub.modules.insights.domain.rules import note_by_rules, restock_by_rules, waste_by_rules
from resthub.modules.insights.domain.sales import (
    CatalogDish,
    OrderFact,
    SoldDish,
    daily_sales,
    dish_margins,
    hourly_heatmap,
    payment_mix,
    summarize,
    top_dishes,
    waiter_performance,
)
from resthub.modules.insights.domain.stock import (
    IngredientFlow,
    RestockFacts,
    StockFact,
    UsageTrend,
    WasteFact,
    low_stock,
    restock_facts,
    waste_report,
)

LIMA = "America/Lima"
LUNES = date(2026, 9, 21)


def _pedido(
    order_id: int,
    total: str,
    *,
    dia: date = LUNES,
    hora_utc: datetime | None = None,
    estado: str = "paid",
    medio: str | None = "cash",
    mesero: int = 1,
) -> OrderFact:
    return OrderFact(
        id=order_id,
        business_date=dia,
        created_at=hora_utc or datetime(2026, 9, 21, 18, 0, tzinfo=UTC),
        status=estado,
        total=Decimal(total),
        payment_method=medio if estado == "paid" else None,
        waiter_id=mesero,
    )


# -- Rango -------------------------------------------------------------------


def test_sin_fechas_el_rango_son_los_ultimos_treinta_dias_del_local() -> None:
    # Las 03:00 UTC del 22 todavía son las 22:00 del 21 en Lima.
    ahora = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)

    rango = resolve_range(None, None, ahora, LIMA)

    assert rango.end == date(2026, 9, 21)
    assert rango.start == date(2026, 8, 23)
    assert rango.days == 30


def test_un_rango_al_reves_o_demasiado_largo_se_rechaza() -> None:
    with pytest.raises(InvalidPeriod):
        DateRange(start=date(2026, 9, 2), end=date(2026, 9, 1))
    with pytest.raises(InvalidPeriod):
        DateRange(start=date(2025, 1, 1), end=date(2026, 9, 1))


def test_el_rango_anterior_tiene_el_mismo_largo_y_termina_justo_antes() -> None:
    rango = DateRange(start=date(2026, 9, 1), end=date(2026, 9, 7))

    assert rango.previous() == DateRange(start=date(2026, 8, 25), end=date(2026, 8, 31))


def test_la_ventana_del_rango_empieza_a_medianoche_del_local() -> None:
    desde, hasta = DateRange(start=LUNES, end=LUNES).window(LIMA)

    assert desde.astimezone(UTC) == datetime(2026, 9, 21, 5, 0, tzinfo=UTC)
    assert hasta.astimezone(UTC) == datetime(2026, 9, 22, 5, 0, tzinfo=UTC)


# -- Ventas ------------------------------------------------------------------


def test_el_resumen_suma_solo_lo_pagado_y_cuenta_aparte_lo_cancelado() -> None:
    resumen = summarize(
        [
            _pedido(1, "28.00"),
            _pedido(2, "15.50"),
            _pedido(3, "10.00"),
            _pedido(4, "40.00", estado="cancelled"),
        ]
    )

    assert resumen.sales == Decimal("53.50")
    assert resumen.paid_orders == 3
    # 53.50 / 3 = 17.8333…, redondeado al céntimo.
    assert resumen.average_ticket == Decimal("17.83")
    assert resumen.cancelled_orders == 1
    assert resumen.cancelled_amount == Decimal("40.00")


def test_sin_ventas_el_ticket_promedio_es_cero_y_no_divide_entre_cero() -> None:
    assert summarize([]).average_ticket == Decimal("0.00")


def test_las_ventas_por_dia_incluyen_los_dias_sin_ventas() -> None:
    rango = DateRange(start=LUNES, end=LUNES + timedelta(days=2))
    puntos = daily_sales(
        [_pedido(1, "20.00"), _pedido(2, "10.00"), _pedido(3, "30.00", dia=LUNES + timedelta(2))],
        rango,
    )

    assert [(p.day, p.sales, p.paid_orders) for p in puntos] == [
        (LUNES, Decimal("30.00"), 2),
        (LUNES + timedelta(days=1), Decimal("0.00"), 0),
        (LUNES + timedelta(days=2), Decimal("30.00"), 1),
    ]
    assert puntos[0].average_ticket == Decimal("15.00")


def test_el_mapa_de_calor_usa_la_hora_del_local_y_no_la_de_utc() -> None:
    # Martes 01:30 UTC es lunes 20:30 en Lima: cena del lunes.
    cena = _pedido(1, "50.00", hora_utc=datetime(2026, 9, 22, 1, 30, tzinfo=UTC))
    # Lunes 17:10 UTC es lunes 12:10 en Lima: almuerzo.
    almuerzo = _pedido(2, "20.00", hora_utc=datetime(2026, 9, 21, 17, 10, tzinfo=UTC))
    rango = DateRange(start=LUNES, end=LUNES + timedelta(days=13))

    celdas = {(c.weekday, c.hour): c for c in hourly_heatmap([cena, almuerzo], rango, LIMA)}

    assert len(celdas) == 7 * 24
    assert celdas[(0, 20)].sales == Decimal("50.00")
    assert celdas[(0, 12)].paid_orders == 1
    assert celdas[(1, 1)].paid_orders == 0
    # El rango tiene dos lunes: el promedio de esa hora es la mitad.
    assert celdas[(0, 20)].average_sales == Decimal("25.00")
    assert celdas[(0, 20)].weekday_label == "Lunes"


def test_los_mas_vendidos_se_ordenan_por_cantidad_y_luego_por_ingresos() -> None:
    vendidos = [
        SoldDish(1, "Chicha", 10, Decimal("50")),
        SoldDish(2, "Lomo", 10, Decimal("320")),
        SoldDish(3, "Ceviche", 12, Decimal("384")),
    ]

    assert [d.name for d in top_dishes(vendidos, 2)] == ["Ceviche", "Lomo"]


def test_el_margen_por_plato_usa_el_costo_de_receta() -> None:
    carta = [
        CatalogDish(1, "Lomo saltado", "Fondos", Decimal("32.00"), True, Decimal("12.3456")),
        CatalogDish(2, "Choclo con queso", "Entradas", Decimal("10.00"), True, None),
        CatalogDish(3, "Plato retirado", "Fondos", Decimal("20.00"), False, Decimal("5")),
        CatalogDish(4, "Retirado vendido", "Fondos", Decimal("20.00"), False, Decimal("5")),
    ]
    vendidos = [
        SoldDish(1, "Lomo saltado", 3, Decimal("96.00")),
        SoldDish(4, "x", 1, Decimal("20")),
    ]

    filas = {fila.menu_item_id: fila for fila in dish_margins(carta, vendidos)}

    lomo = filas[1]
    assert lomo.recipe_cost == Decimal("12.35")
    assert lomo.unit_margin == Decimal("19.65")
    assert lomo.margin_percent == Decimal("61.4")
    assert lomo.estimated_cost == Decimal("37.04")
    assert lomo.gross_margin == Decimal("58.96")
    assert filas[2].recipe_cost is None and filas[2].gross_margin is None
    # Un plato retirado aparece solo si se vendió en el rango.
    assert 3 not in filas and 4 in filas


def test_los_medios_de_pago_reparten_el_cien_por_ciento() -> None:
    mezcla = payment_mix(
        [
            _pedido(1, "30.00", medio="yape"),
            _pedido(2, "50.00", medio="yape"),
            _pedido(3, "20.00", medio="cash"),
            _pedido(4, "99.00", estado="cancelled"),
        ]
    )

    assert [(m.method, m.amount, m.share_percent, m.paid_orders) for m in mezcla] == [
        ("yape", Decimal("80.00"), Decimal("80.0"), 2),
        ("cash", Decimal("20.00"), Decimal("20.0"), 1),
    ]
    assert mezcla[0].label == "Yape"


def test_el_rendimiento_por_mesero_separa_cobrados_y_cancelados() -> None:
    filas = waiter_performance(
        [
            _pedido(1, "30.00", mesero=7),
            _pedido(2, "10.00", mesero=7),
            _pedido(3, "15.00", mesero=8, estado="cancelled"),
        ],
        {7: "Luis", 8: "Carla"},
    )

    assert [
        (f.name, f.paid_orders, f.sales, f.average_ticket, f.cancelled_orders) for f in filas
    ] == [
        ("Luis", 2, Decimal("40.00"), Decimal("20.00"), 0),
        ("Carla", 0, Decimal("0.00"), Decimal("0.00"), 1),
    ]


# -- Almacén -----------------------------------------------------------------


def test_bajo_minimo_ordena_del_mas_comprometido_al_menos() -> None:
    hechos = [
        StockFact(1, "Culantro", "g", Decimal("150"), Decimal("200")),
        StockFact(2, "Cusqueña", "unit", Decimal("2"), Decimal("12")),
        StockFact(3, "Arroz", "g", Decimal("5000"), Decimal("5000")),
    ]

    assert [f.name for f in low_stock(hechos)] == ["Cusqueña", "Culantro"]


def _hechos(
    stock: str,
    minimo: str = "0",
    semana: str = "0",
    mes: str = "0",
    merma: str = "0",
) -> RestockFacts:
    ahora = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    return restock_facts(
        StockFact(1, "Lomo de res", "g", Decimal(stock), Decimal(minimo)),
        IngredientFlow(
            consumed_short=Decimal(semana),
            consumed_long=Decimal(mes),
            wasted_long=Decimal(merma),
            first_movement_at=ahora - timedelta(days=90),
            last_purchase_at=ahora - timedelta(days=3),
        ),
        ahora,
    )


def test_los_numeros_de_reposicion_se_calculan_en_codigo() -> None:
    hechos = _hechos("3000", minimo="2000", semana="14000", mes="42000", merma="2000")

    assert hechos.daily_use_short == Decimal("2000.000")
    assert hechos.daily_use_long == Decimal("1500.000")
    assert hechos.coverage_days == Decimal("1.5")
    assert hechos.trend is UsageTrend.RISING
    assert hechos.usage_change_percent == Decimal("33.3")
    # 2 kg de merma sobre 44 kg que salieron.
    assert hechos.waste_share == Decimal("0.045")
    assert hechos.days_since_last_purchase == 3


def test_un_insumo_nuevo_promedia_sobre_los_dias_que_lleva() -> None:
    ahora = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    hechos = restock_facts(
        StockFact(1, "Ají limo", "g", Decimal("500"), Decimal("0")),
        IngredientFlow(
            consumed_short=Decimal("300"),
            consumed_long=Decimal("300"),
            first_movement_at=ahora - timedelta(days=3),
        ),
        ahora,
    )

    assert hechos.daily_use_short == Decimal("100.000")
    assert hechos.daily_use_long == Decimal("100.000")


@pytest.mark.parametrize(
    ("hechos", "accion", "urgencia"),
    [
        (_hechos("0", semana="700"), RestockAction.BUY_TODAY, Urgency.CRITICAL),
        (_hechos("500", semana="7000"), RestockAction.BUY_TODAY, Urgency.CRITICAL),
        (_hechos("1500", semana="7000"), RestockAction.BUY_TODAY, Urgency.HIGH),
        (_hechos("4000", semana="7000"), RestockAction.BUY_THIS_WEEK, Urgency.MEDIUM),
        (
            _hechos("9000", minimo="10000", semana="7000"),
            RestockAction.BUY_THIS_WEEK,
            Urgency.MEDIUM,
        ),
        (_hechos("20000", minimo="2000", semana="7000"), RestockAction.WAIT, Urgency.LOW),
        (_hechos("100", minimo="0"), RestockAction.WAIT, Urgency.LOW),
        (
            _hechos("20000", semana="7000", mes="28000", merma="6000"),
            RestockAction.REVIEW_WASTE,
            Urgency.LOW,
        ),
    ],
)
def test_reglas_de_reposicion(
    hechos: RestockFacts, accion: RestockAction, urgencia: Urgency
) -> None:
    resultado, detalle = restock_by_rules(hechos)

    assert resultado.action is accion
    assert resultado.urgency is urgencia
    assert detalle["rule"]


def test_la_explicacion_sale_de_los_numeros() -> None:
    hechos = _hechos("5600", minimo="6000", semana="14000", mes="56000")
    veredicto = Verdict(outcome=restock_by_rules(hechos)[0], engine=Engine.RULES)

    texto = explain_restock(hechos, veredicto)

    assert "Quedan 5,6 kg de Lomo de res" in texto
    assert "por debajo del mínimo de 6 kg" in texto
    assert "alcanza para 2,8 días" in texto
    assert "Conviene comprarlo en los próximos días." in texto
    assert amount(Decimal("3"), "unit") == "3 unidades"
    assert amount(Decimal("250"), "ml") == "250 ml"


@pytest.mark.parametrize(
    ("nota", "alergia", "tipo"),
    [
        ("Es ALÉRGICO al maní", True, NoteType.ALLERGY),
        ("sin cebolla, y la señora es alergica a los mariscos", True, NoteType.ALLERGY),
        ("celíaco: nada con gluten", True, NoteType.ALLERGY),
        ("vegetariana", True, NoteType.ALLERGY),
        ("sin cebolla por favor", False, NoteType.PREFERENCE),
        ("término medio", False, NoteType.PREFERENCE),
        ("urgente, el cliente está apurado", False, NoteType.PRIORITY),
        ("para la mesa del fondo", False, NoteType.OTHER),
    ],
)
def test_reglas_de_notas_de_pedido(nota: str, alergia: bool, tipo: NoteType) -> None:
    resultado, _ = note_by_rules(nota)

    assert resultado.mentions_allergy is alergia
    assert resultado.note_type is tipo


@pytest.mark.parametrize(
    ("motivo", "causa"),
    [
        ("Se venció el queso", WasteCause.EXPIRATION),
        ("olía mal, se malogró con el calor", WasteCause.EXPIRATION),
        ("El cliente lo devolvió porque estaba salado", WasteCause.CUSTOMER_RETURN),
        ("Se quemó el arroz", WasteCause.PREPARATION_ERROR),
        ("Se cayó la olla al piso", WasteCause.MISHANDLING),
        ("se cortó la luz y la refrigeradora se apagó", WasteCause.MISHANDLING),
        ("conteo", WasteCause.OTHER),
    ],
)
def test_reglas_de_merma(motivo: str, causa: WasteCause) -> None:
    assert waste_by_rules(motivo)[0].cause is causa


def test_el_reporte_de_mermas_agrupa_por_insumo_y_por_causa() -> None:
    momento = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)

    def merma(mid: int, iid: int, costo: str, causa: WasteCause | None) -> WasteFact:
        return WasteFact(
            mid, iid, f"insumo {iid}", "g", Decimal("100"), Decimal(costo), "x", momento, causa
        )

    reporte = waste_report(
        [
            merma(1, 1, "3.00", WasteCause.EXPIRATION),
            merma(2, 1, "1.00", WasteCause.EXPIRATION),
            merma(3, 2, "4.00", None),
        ]
    )

    assert reporte.events == 3
    assert reporte.total_cost == Decimal("8.00")
    assert reporte.pending_classification == 1
    assert [(f.ingredient_id, f.quantity, f.cost) for f in reporte.by_ingredient] == [
        (1, Decimal("200.000"), Decimal("4.00")),
        (2, Decimal("100.000"), Decimal("4.00")),
    ]
    assert [(c.cause, c.share_percent, c.label) for c in reporte.by_cause] == [
        (WasteCause.EXPIRATION, Decimal("50.0"), "Vencimiento"),
        (None, Decimal("50.0"), "Sin clasificar"),
    ]
