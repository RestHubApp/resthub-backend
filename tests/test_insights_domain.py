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
from resthub.modules.insights.domain.exceptions import InvalidPeriodError
from resthub.modules.insights.domain.explanations import (
    amount,
    explain_restock,
    with_decimal_point,
)
from resthub.modules.insights.domain.period import DateRange, resolve_range, supported_bounds
from resthub.modules.insights.domain.rules import note_by_rules, restock_by_rules, waste_by_rules
from resthub.modules.insights.domain.sales import (
    CatalogDish,
    OrderFact,
    PaymentFact,
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
MONDAY = date(2026, 9, 21)


def _order(
    order_id: int,
    total: str,
    *,
    day: date = MONDAY,
    utc_time: datetime | None = None,
    status: str = "paid",
    method: str | None = "cash",
    waiter: int = 1,
) -> OrderFact:
    return OrderFact(
        id=order_id,
        business_date=day,
        created_at=utc_time or datetime(2026, 9, 21, 18, 0, tzinfo=UTC),
        status=status,
        total=Decimal(total),
        payment_method=method if status == "paid" else None,
        waiter_id=waiter,
    )


# -- Rango -------------------------------------------------------------------


def test_without_dates_the_range_is_the_last_thirty_days_of_the_restaurant() -> None:
    # Las 03:00 UTC del 22 todavía son las 22:00 del 21 en Lima.
    now = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)

    date_range = resolve_range(None, None, now, LIMA)

    assert date_range.end == date(2026, 9, 21)
    assert date_range.start == date(2026, 8, 23)
    assert date_range.days == 30


def test_reversed_or_too_long_range_is_rejected() -> None:
    with pytest.raises(InvalidPeriodError):
        DateRange(start=date(2026, 9, 2), end=date(2026, 9, 1))
    with pytest.raises(InvalidPeriodError):
        DateRange(start=date(2025, 1, 1), end=date(2026, 9, 1))


def test_dates_outside_the_window_around_today_are_rejected_with_the_computed_range() -> None:
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    message = "Las fechas deben estar entre 1976-09-01 y 2031-09-01."
    for given in (date.min, date.max, date(1976, 8, 31), date(2031, 9, 2)):
        with pytest.raises(InvalidPeriodError, match=message):
            resolve_range(None, given, now, "America/Lima")
        with pytest.raises(InvalidPeriodError, match=message):
            resolve_range(given, None, now, "America/Lima")


def test_the_window_moves_with_today_and_keeps_its_edges() -> None:
    assert supported_bounds(date(2026, 9, 1)) == (date(1976, 9, 1), date(2031, 9, 1))
    assert supported_bounds(date(2040, 2, 29)) == (date(1990, 2, 28), date(2045, 2, 28))
    now = datetime(2060, 1, 10, 12, tzinfo=UTC)
    assert resolve_range(date(2010, 1, 10), date(2010, 1, 10), now, LIMA).start == date(2010, 1, 10)
    with pytest.raises(InvalidPeriodError, match="entre 2010-01-10 y 2065-01-10"):
        resolve_range(date(2010, 1, 9), date(2010, 1, 9), now, LIMA)


def test_date_range_guards_against_overflow_without_a_clock() -> None:
    for given in (date.min, date.max):
        with pytest.raises(InvalidPeriodError, match="puede representar"):
            DateRange(start=given, end=given)


def test_previous_range_has_the_same_length_and_ends_right_before() -> None:
    date_range = DateRange(start=date(2026, 9, 1), end=date(2026, 9, 7))

    assert date_range.previous() == DateRange(start=date(2026, 8, 25), end=date(2026, 8, 31))


def test_range_window_starts_at_restaurant_midnight() -> None:
    window_start, window_end = DateRange(start=MONDAY, end=MONDAY).window(LIMA)

    assert window_start.astimezone(UTC) == datetime(2026, 9, 21, 5, 0, tzinfo=UTC)
    assert window_end.astimezone(UTC) == datetime(2026, 9, 22, 5, 0, tzinfo=UTC)


# -- Ventas ------------------------------------------------------------------


def test_summary_adds_only_paid_and_counts_cancelled_apart() -> None:
    summary = summarize(
        [
            _order(1, "28.00"),
            _order(2, "15.50"),
            _order(3, "10.00"),
            _order(4, "40.00", status="cancelled"),
        ]
    )

    assert summary.sales == Decimal("53.50")
    assert summary.paid_orders == 3
    # 53.50 / 3 = 17.8333…, redondeado al céntimo.
    assert summary.average_ticket == Decimal("17.83")
    assert summary.cancelled_orders == 1
    assert summary.cancelled_amount == Decimal("40.00")


def test_without_sales_average_ticket_is_zero_and_does_not_divide_by_zero() -> None:
    assert summarize([]).average_ticket == Decimal("0.00")


def test_daily_sales_include_days_without_sales() -> None:
    date_range = DateRange(start=MONDAY, end=MONDAY + timedelta(days=2))
    points = daily_sales(
        [_order(1, "20.00"), _order(2, "10.00"), _order(3, "30.00", day=MONDAY + timedelta(2))],
        date_range,
    )

    assert [(p.day, p.sales, p.paid_orders) for p in points] == [
        (MONDAY, Decimal("30.00"), 2),
        (MONDAY + timedelta(days=1), Decimal("0.00"), 0),
        (MONDAY + timedelta(days=2), Decimal("30.00"), 1),
    ]
    assert points[0].average_ticket == Decimal("15.00")


def test_heatmap_uses_restaurant_time_not_utc() -> None:
    # Martes 01:30 UTC es lunes 20:30 en Lima: cena del lunes.
    dinner = _order(1, "50.00", utc_time=datetime(2026, 9, 22, 1, 30, tzinfo=UTC))
    # Lunes 17:10 UTC es lunes 12:10 en Lima: almuerzo.
    lunch = _order(2, "20.00", utc_time=datetime(2026, 9, 21, 17, 10, tzinfo=UTC))
    date_range = DateRange(start=MONDAY, end=MONDAY + timedelta(days=13))

    cells = {(c.weekday, c.hour): c for c in hourly_heatmap([dinner, lunch], date_range, LIMA)}

    assert len(cells) == 7 * 24
    assert cells[(0, 20)].sales == Decimal("50.00")
    assert cells[(0, 12)].paid_orders == 1
    assert cells[(1, 1)].paid_orders == 0
    # El rango tiene dos lunes: el promedio de esa hora es la mitad.
    assert cells[(0, 20)].average_sales == Decimal("25.00")
    assert cells[(0, 20)].weekday_label == "Lunes"


def test_top_sellers_sort_by_quantity_then_by_revenue() -> None:
    sold = [
        SoldDish(1, "Chicha", 10, Decimal(50)),
        SoldDish(2, "Lomo", 10, Decimal(320)),
        SoldDish(3, "Ceviche", 12, Decimal(384)),
    ]

    assert [d.name for d in top_dishes(sold, 2)] == ["Ceviche", "Lomo"]


def test_dish_margin_uses_the_recipe_cost() -> None:
    menu = [
        CatalogDish(1, "Lomo saltado", "Fondos", Decimal("32.00"), True, Decimal("12.3456")),
        CatalogDish(2, "Choclo con queso", "Entradas", Decimal("10.00"), True, None),
        CatalogDish(3, "Plato retirado", "Fondos", Decimal("20.00"), False, Decimal(5)),
        CatalogDish(4, "Retirado vendido", "Fondos", Decimal("20.00"), False, Decimal(5)),
    ]
    sold = [
        SoldDish(1, "Lomo saltado", 3, Decimal("96.00")),
        SoldDish(4, "x", 1, Decimal(20)),
    ]

    rows = {row.menu_item_id: row for row in dish_margins(menu, sold)}

    lomo = rows[1]
    assert lomo.recipe_cost == Decimal("12.35")
    assert lomo.unit_margin == Decimal("19.65")
    assert lomo.margin_percent == Decimal("61.4")
    assert lomo.estimated_cost == Decimal("37.04")
    assert lomo.gross_margin == Decimal("58.96")
    assert rows[2].recipe_cost is None
    assert rows[2].gross_margin is None
    # Un plato retirado aparece solo si se vendió en el rango.
    assert 3 not in rows
    assert 4 in rows


def _payment(
    order_id: int, amount: str, method: str, tip: str = "0.00", waiter: int = 1
) -> PaymentFact:
    return PaymentFact(
        order_id=order_id,
        method=method,
        amount=Decimal(amount),
        tip=Decimal(tip),
        waiter_id=waiter,
    )


def test_payment_methods_split_one_hundred_percent() -> None:
    mix = payment_mix(
        [
            _payment(1, "30.00", "yape"),
            _payment(2, "50.00", "yape"),
            _payment(3, "20.00", "cash", tip="5.00"),
        ]
    )

    assert [(m.method, m.amount, m.share_percent, m.paid_orders) for m in mix] == [
        ("yape", Decimal("80.00"), Decimal("80.0"), 2),
        ("cash", Decimal("20.00"), Decimal("20.0"), 1),
    ]
    assert mix[0].label == "Yape"


def test_split_payment_adds_each_part_to_its_method() -> None:
    mix = payment_mix([_payment(1, "40.00", "cash"), _payment(1, "60.00", "yape")])

    assert [(m.method, m.amount, m.paid_orders) for m in mix] == [
        ("yape", Decimal("60.00"), 1),
        ("cash", Decimal("40.00"), 1),
    ]


def test_waiter_performance_separates_charged_and_cancelled() -> None:
    rows = waiter_performance(
        [
            _order(1, "30.00", waiter=7),
            _order(2, "10.00", waiter=7),
            _order(3, "15.00", waiter=8, status="cancelled"),
        ],
        {7: "Luis", 8: "Carla"},
        [_payment(1, "30.00", "cash", tip="3.00", waiter=7)],
    )

    assert [
        (f.name, f.paid_orders, f.sales, f.average_ticket, f.cancelled_orders, f.tips) for f in rows
    ] == [
        ("Luis", 2, Decimal("40.00"), Decimal("20.00"), 0, Decimal("3.00")),
        ("Carla", 0, Decimal("0.00"), Decimal("0.00"), 1, Decimal("0.00")),
    ]


# -- Almacén -----------------------------------------------------------------


def test_below_minimum_sorts_from_most_to_least_at_risk() -> None:
    facts = [
        StockFact(1, "Culantro", "g", Decimal(150), Decimal(200)),
        StockFact(2, "Cusqueña", "unit", Decimal(2), Decimal(12)),
        StockFact(3, "Arroz", "g", Decimal(5000), Decimal(5000)),
    ]

    assert [f.name for f in low_stock(facts)] == ["Cusqueña", "Culantro"]


def _facts(
    stock: str,
    minimum: str = "0",
    week: str = "0",
    month: str = "0",
    waste: str = "0",
) -> RestockFacts:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    return restock_facts(
        StockFact(1, "Lomo de res", "g", Decimal(stock), Decimal(minimum)),
        IngredientFlow(
            consumed_short=Decimal(week),
            consumed_long=Decimal(month),
            wasted_long=Decimal(waste),
            first_movement_at=now - timedelta(days=90),
            last_purchase_at=now - timedelta(days=3),
        ),
        now,
    )


def test_restock_numbers_are_computed_in_code() -> None:
    facts = _facts("3000", minimum="2000", week="14000", month="42000", waste="2000")

    assert facts.daily_use_short == Decimal("2000.000")
    assert facts.daily_use_long == Decimal("1500.000")
    assert facts.coverage_days == Decimal("1.5")
    assert facts.trend is UsageTrend.RISING
    assert facts.usage_change_percent == Decimal("33.3")
    # 2 kg de merma sobre 44 kg que salieron.
    assert facts.waste_share == Decimal("0.045")
    assert facts.days_since_last_purchase == 3


def test_new_ingredient_averages_over_the_days_it_has_existed() -> None:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    facts = restock_facts(
        StockFact(1, "Ají limo", "g", Decimal(500), Decimal(0)),
        IngredientFlow(
            consumed_short=Decimal(300),
            consumed_long=Decimal(300),
            first_movement_at=now - timedelta(days=3),
        ),
        now,
    )

    assert facts.daily_use_short == Decimal("100.000")
    assert facts.daily_use_long == Decimal("100.000")


@pytest.mark.parametrize(
    ("facts", "action", "urgency"),
    [
        (_facts("0", week="700"), RestockAction.BUY_TODAY, Urgency.CRITICAL),
        (_facts("500", week="7000"), RestockAction.BUY_TODAY, Urgency.CRITICAL),
        (_facts("1500", week="7000"), RestockAction.BUY_TODAY, Urgency.HIGH),
        (_facts("4000", week="7000"), RestockAction.BUY_THIS_WEEK, Urgency.MEDIUM),
        (
            _facts("9000", minimum="10000", week="7000"),
            RestockAction.BUY_THIS_WEEK,
            Urgency.MEDIUM,
        ),
        (_facts("20000", minimum="2000", week="7000"), RestockAction.WAIT, Urgency.LOW),
        (_facts("100", minimum="0"), RestockAction.WAIT, Urgency.LOW),
        (
            _facts("20000", week="7000", month="28000", waste="6000"),
            RestockAction.REVIEW_WASTE,
            Urgency.LOW,
        ),
    ],
)
def test_restock_rules(facts: RestockFacts, action: RestockAction, urgency: Urgency) -> None:
    result, detail = restock_by_rules(facts)

    assert result.action is action
    assert result.urgency is urgency
    assert detail["rule"]


def test_explanation_comes_from_the_numbers() -> None:
    facts = _facts("5600", minimum="6000", week="14000", month="56000")
    verdict = Verdict(outcome=restock_by_rules(facts)[0], engine=Engine.RULES)

    text = explain_restock(facts, verdict)

    assert "Quedan 5.6 kg de Lomo de res" in text
    assert "por debajo del mínimo de 6 kg" in text
    assert "alcanza para 2.8 días" in text
    assert "Conviene comprarlo en los próximos días." in text
    assert amount(Decimal(3), "unit") == "3 unidades"
    assert amount(Decimal(250), "ml") == "250 ml"
    assert amount(Decimal(1960), "g") == "1.96 kg"


def test_saved_explanation_with_decimal_comma_reads_with_a_point() -> None:
    saved = "Quedan 1,76 kg de Papa, por debajo del mínimo de 2 kg; alcanza para 0,5 días."

    assert with_decimal_point(saved) == (
        "Quedan 1.76 kg de Papa, por debajo del mínimo de 2 kg; alcanza para 0.5 días."
    )


@pytest.mark.parametrize(
    ("note", "allergy", "kind"),
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
def test_order_note_rules(note: str, allergy: bool, kind: NoteType) -> None:
    result, _ = note_by_rules(note)

    assert result.mentions_allergy is allergy
    assert result.note_type is kind


@pytest.mark.parametrize(
    ("reason", "cause"),
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
def test_waste_rules(reason: str, cause: WasteCause) -> None:
    assert waste_by_rules(reason)[0].cause is cause


def test_waste_report_groups_by_ingredient_and_by_cause() -> None:
    timestamp = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)

    def waste(mid: int, iid: int, cost: str, cause: WasteCause | None) -> WasteFact:
        return WasteFact(
            mid, iid, f"insumo {iid}", "g", Decimal(100), Decimal(cost), "x", timestamp, cause
        )

    report = waste_report(
        [
            waste(1, 1, "3.00", WasteCause.EXPIRATION),
            waste(2, 1, "1.00", WasteCause.EXPIRATION),
            waste(3, 2, "4.00", None),
        ]
    )

    assert report.events == 3
    assert report.total_cost == Decimal("8.00")
    assert report.pending_classification == 1
    assert [(f.ingredient_id, f.quantity, f.cost) for f in report.by_ingredient] == [
        (1, Decimal("200.000"), Decimal("4.00")),
        (2, Decimal("100.000"), Decimal("4.00")),
    ]
    assert [(c.cause, c.share_percent, c.label) for c in report.by_cause] == [
        (WasteCause.EXPIRATION, Decimal("50.0"), "Vencimiento"),
        (None, Decimal("50.0"), "Sin clasificar"),
    ]
