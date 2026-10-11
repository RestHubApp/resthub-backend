"""Indicadores por HTTP: cuentas, zona horaria, decisiones, permisos y aislamiento."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.background import BackgroundJobs
from resthub.core.realtime_broker import LocalBroker
from resthub.modules.insights.adapters.ai.jev_engine import (
    RESTOCK_QUESTIONS,
    JevDecisionEngine,
)
from resthub.modules.insights.adapters.ai.rule_based_engine import RuleBasedDecisionEngine
from resthub.modules.insights.adapters.ai.selector import DecisionEngineSelector
from resthub.modules.insights.adapters.api.dependencies import get_decision_engine
from resthub.modules.insights.domain.decisions import (
    Engine,
    KitchenNote,
    NoteOutcome,
    NoteType,
    Verdict,
)
from resthub.modules.orders.adapters.persistence.sqlalchemy_order_repository import (
    SqlAlchemyOrderRepository,
)
from resthub.modules.orders.domain.orders import (
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    Payment,
    PaymentMethod,
)
from tests.builders import SeededMenu, seed_menu
from tests.conftest import StaffedRestaurant, authorization_for, url

INSIGHTS_URL = url("insights")
ORDERS_URL = url("orders")
INVENTORY_URL = url("inventory")
WEEK = {"date_from": "2026-09-21", "date_to": "2026-09-27"}


async def _sale(
    session: AsyncSession,
    restaurant: StaffedRestaurant,
    menu: SeededMenu,
    number: int,
    day: date,
    opened_utc: datetime,
    *dishes: tuple[int, str, str, int],
    status: OrderStatus = OrderStatus.PAID,
    method: PaymentMethod | None = PaymentMethod.CASH,
    waiter: int | None = None,
) -> Order:
    """Un pedido ya cerrado, escrito directo en la base con la hora que se quiera."""
    items = [
        OrderItem(menu_item_id=dish, name=name, unit_price=Decimal(price), quantity=q)
        for dish, name, price, q in dishes
    ]
    waiter_id = waiter or restaurant.waiter.id or 0
    total = sum((item.subtotal for item in items), Decimal("0.00"))
    order = Order(
        restaurant_id=restaurant.id,
        number=number,
        business_date=day,
        type=OrderType.TAKEAWAY,
        waiter_id=waiter_id,
        status=status,
        items=items,
        payments=(
            [Payment(method=method, amount=total, received_by=waiter_id, created_at=opened_utc)]
            if status is OrderStatus.PAID and method is not None
            else []
        ),
        cancel_reason="El cliente se fue" if status is OrderStatus.CANCELLED else "",
        created_at=opened_utc,
        updated_at=opened_utc,
        paid_at=opened_utc if status is OrderStatus.PAID else None,
    )
    saved = await SqlAlchemyOrderRepository(session).add(order)
    await session.commit()
    return saved


@pytest.fixture
async def menu_a(session: AsyncSession, restaurant_a: StaffedRestaurant) -> SeededMenu:
    return await seed_menu(session, restaurant_a.id)


@pytest.fixture
async def sales_a(
    session: AsyncSession, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    """Una semana armada a mano en el local A (Lima, UTC-5).

    - Lunes 21, 12:30 local: 2 lomos (56.00) en efectivo.
    - Lunes 21, 23:30 local (martes 04:30 UTC): 1 ají (22.00) por Yape. Cuenta
      el lunes y a las 23 h, no el martes.
    - Miércoles 23: 1 lomo y 2 chichas (39.00) por Yape, del encargado.
    - Miércoles 23: cancelado (28.00).
    - Domingo 20, fuera del rango: 1 lomo (28.00), es el período anterior.
    """
    lomo = (menu_a.lomo, "Lomo saltado", "28.00")
    aji = (menu_a.aji, "Ají de gallina", "22.00")
    chicha = (menu_a.chicha, "Chicha morada", "5.50")
    await _sale(
        session,
        restaurant_a,
        menu_a,
        1,
        date(2026, 9, 21),
        datetime(2026, 9, 21, 17, 30, tzinfo=UTC),
        (*lomo, 2),
    )
    await _sale(
        session,
        restaurant_a,
        menu_a,
        2,
        date(2026, 9, 21),
        datetime(2026, 9, 22, 4, 30, tzinfo=UTC),
        (*aji, 1),
        method=PaymentMethod.YAPE,
    )
    await _sale(
        session,
        restaurant_a,
        menu_a,
        1,
        date(2026, 9, 23),
        datetime(2026, 9, 23, 20, 0, tzinfo=UTC),
        (*lomo, 1),
        (*chicha, 2),
        method=PaymentMethod.YAPE,
        waiter=restaurant_a.admin.id,
    )
    await _sale(
        session,
        restaurant_a,
        menu_a,
        2,
        date(2026, 9, 23),
        datetime(2026, 9, 23, 21, 0, tzinfo=UTC),
        (*lomo, 1),
        status=OrderStatus.CANCELLED,
    )
    await _sale(
        session,
        restaurant_a,
        menu_a,
        1,
        date(2026, 9, 20),
        datetime(2026, 9, 20, 18, 0, tzinfo=UTC),
        (*lomo, 1),
    )


async def _get(
    client: AsyncClient, restaurant: StaffedRestaurant, path: str, **params: Any
) -> dict[str, Any]:
    response = await client.get(
        f"{INSIGHTS_URL}/{path}", params=params, headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _post(client: AsyncClient, restaurant: StaffedRestaurant, path: str) -> dict[str, Any]:
    response = await client.post(
        f"{INSIGHTS_URL}/{path}", headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 200, response.text
    return response.json()


# -- Ventas ------------------------------------------------------------------


@pytest.mark.usefixtures("sales_a")
async def test_summary_with_average_ticket_cancelled_and_previous_period(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    summary = await _get(client, restaurant_a, "summary", **WEEK)

    assert summary["period"] == {
        "date_from": "2026-09-21",
        "date_to": "2026-09-27",
        "days": 7,
        "timezone": "America/Lima",
    }
    assert summary["sales"] == "117.00"
    assert summary["paid_orders"] == 3
    assert summary["average_ticket"] == "39.00"
    assert summary["cancelled_orders"] == 1
    assert summary["cancelled_amount"] == "28.00"
    assert summary["previous"]["date_from"] == "2026-09-14"
    assert summary["previous"]["sales"] == "28.00"
    # 117 contra 28: +317.9 %.
    assert summary["sales_change_percent"] == "317.9"


@pytest.mark.usefixtures("sales_a")
async def test_sales_per_day_count_the_restaurant_day(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    days = (await _get(client, restaurant_a, "sales/daily", **WEEK))["days"]

    assert len(days) == 7
    assert days[0] == {
        "date": "2026-09-21",
        "sales": "78.00",
        "paid_orders": 2,
        "average_ticket": "39.00",
    }
    assert days[1]["sales"] == "0.00"
    assert days[2]["sales"] == "39.00"


@pytest.mark.usefixtures("sales_a")
async def test_heatmap_places_midnight_sale_in_its_local_hour(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    data = await _get(client, restaurant_a, "sales/hourly", **WEEK)
    cells = {(c["weekday"], c["hour"]): c for c in data["cells"]}

    assert len(cells) == 168
    assert cells[(0, 12)]["sales"] == "56.00"
    assert cells[(0, 23)]["sales"] == "22.00"
    assert cells[(1, 4)]["sales"] == "0.00"
    assert cells[(2, 15)]["sales"] == "39.00"
    assert data["peak"]["weekday"] == 0
    assert data["peak"]["hour"] == 12


@pytest.mark.usefixtures("sales_a")
async def test_top_selling_dishes_and_payment_methods(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    dishes = (await _get(client, restaurant_a, "dishes/top", limit=2, **WEEK))["dishes"]
    payments = await _get(client, restaurant_a, "payments", **WEEK)

    # Lo cancelado no cuenta como vendido.
    assert [(p["name"], p["quantity"], p["revenue"]) for p in dishes] == [
        ("Lomo saltado", 3, "84.00"),
        ("Chicha morada", 2, "11.00"),
    ]
    assert payments["total"] == "117.00"
    assert [(m["method"], m["amount"], m["share_percent"]) for m in payments["methods"]] == [
        ("yape", "61.00", "52.1"),
        ("cash", "56.00", "47.9"),
    ]


@pytest.mark.usefixtures("sales_a")
async def test_performance_per_waiter(client: AsyncClient, restaurant_a: StaffedRestaurant) -> None:
    waiters = (await _get(client, restaurant_a, "waiters", **WEEK))["waiters"]

    assert [(m["name"], m["paid_orders"], m["sales"], m["cancelled_orders"]) for m in waiters] == [
        ("Luis Torres", 2, "78.00", 1),
        ("Rosa Pérez", 1, "39.00", 0),
    ]


@pytest.mark.usefixtures("sales_a")
async def test_margin_per_dish_with_recipe_cost(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    admin = authorization_for(restaurant_a.admin)
    beef = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": "Lomo de res", "unit": "g", "unit_cost": "0.042"},
        headers=admin,
    )
    await client.put(
        f"{INVENTORY_URL}/recipes/{menu_a.lomo}",
        json={"lines": [{"ingredient_id": beef.json()["id"], "quantity": "200"}]},
        headers=admin,
    )

    dishes = {
        p["name"]: p for p in (await _get(client, restaurant_a, "dishes/margins", **WEEK))["dishes"]
    }

    lomo = dishes["Lomo saltado"]
    assert lomo["recipe_cost"] == "8.40"
    assert lomo["unit_margin"] == "19.60"
    assert lomo["margin_percent"] == "70.0"
    assert lomo["quantity_sold"] == 3
    assert lomo["gross_margin"] == "58.80"
    assert dishes["Ají de gallina"]["recipe_cost"] is None
    # Los platos retirados que no se vendieron no aparecen.
    assert "Seco de res" not in dishes


async def test_invalid_range_responds_422(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(
        f"{INSIGHTS_URL}/summary",
        params={"date_from": "2026-09-10", "date_to": "2026-09-01"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 422
    assert "posterior" in response.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "summary",
        "sales/daily",
        "sales/hourly",
        "payments",
        "waiters",
        "dishes/top",
        "dishes/margins",
        "waste",
    ],
)
@pytest.mark.parametrize(
    "params",
    [
        {"date_to": "0001-01-01"},
        {"date_from": "0001-01-01", "date_to": "0001-01-02"},
        {"date_from": "9999-12-31"},
        {"date_from": "9999-12-30", "date_to": "9999-12-31"},
        {"date_to": "1900-01-01"},
        {"date_from": "2999-12-31"},
    ],
)
async def test_dates_outside_the_window_around_today_respond_422_not_500(
    client: AsyncClient, restaurant_a: StaffedRestaurant, path: str, params: dict[str, str]
) -> None:
    response = await client.get(
        f"{INSIGHTS_URL}/{path}", params=params, headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 422
    assert "Las fechas deben estar entre" in response.json()["detail"]


# -- Almacén -----------------------------------------------------------------


async def _ingredient(
    client: AsyncClient, restaurant: StaffedRestaurant, name: str, **extra: str
) -> int:
    response = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": name, "unit": "g", **extra},
        headers=authorization_for(restaurant.admin),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _move_stock(
    client: AsyncClient, restaurant: StaffedRestaurant, kind: str, **body: Any
) -> None:
    response = await client.post(
        f"{INVENTORY_URL}/{kind}", json=body, headers=authorization_for(restaurant.admin)
    )
    assert response.status_code == 201, response.text


async def test_waste_by_ingredient_and_cause_after_classifying(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    cheese = await _ingredient(client, restaurant_a, "Queso fresco")
    await _move_stock(
        client, restaurant_a, "purchases", ingredient_id=cheese, quantity="2000", unit_cost="0.02"
    )
    await _move_stock(
        client, restaurant_a, "waste", ingredient_id=cheese, quantity="300", reason="Se venció"
    )
    await _move_stock(
        client,
        restaurant_a,
        "waste",
        ingredient_id=cheese,
        quantity="200",
        reason="Se cayó al piso",
    )

    before = await _get(client, restaurant_a, "waste")
    assert before["events"] == 2
    assert before["total_cost"] == "10.00"
    assert before["pending_classification"] == 2
    assert before["by_ingredient"][0]["quantity"] == "500.000"

    run = await _post(client, restaurant_a, "waste/classify")
    assert run["classified"] == 2
    assert {c["cause"] for c in run["by_cause"]} == {"expiration", "mishandling"}
    # Ya clasificadas, una segunda corrida no hace nada.
    assert (await _post(client, restaurant_a, "waste/classify"))["classified"] == 0

    after = await _get(client, restaurant_a, "waste")
    assert after["pending_classification"] == 0
    assert {(c["cause"], c["cost"]) for c in after["by_cause"]} == {
        ("expiration", "6.00"),
        ("mishandling", "4.00"),
    }
    audit = await _get(client, restaurant_a, "ai-decisions", kind="waste_cause")
    assert {d["subject_type"] for d in audit["items"]} == {"stock_movement"}
    assert {d["subject_label"] for d in audit["items"]} == {"Queso fresco"}
    assert {d["order_number"] for d in audit["items"]} == {None}


async def test_ingredients_below_minimum(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    cilantro = await _ingredient(client, restaurant_a, "Culantro", min_stock="200")
    await _ingredient(client, restaurant_a, "Arroz", min_stock="0")
    await _move_stock(
        client, restaurant_a, "purchases", ingredient_id=cilantro, quantity="150", unit_cost="0.01"
    )

    low = await _get(client, restaurant_a, "low-stock")

    assert low == [
        {
            "ingredient_id": cilantro,
            "name": "Culantro",
            "unit": "g",
            "stock": "150.000",
            "min_stock": "200.000",
            "missing": "50.000",
        }
    ]


# -- Reposición --------------------------------------------------------------


async def test_restock_without_decisions_shows_rules_and_refresh_saves_them(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    cilantro = await _ingredient(client, restaurant_a, "Culantro", min_stock="200")
    await _move_stock(
        client, restaurant_a, "purchases", ingredient_id=cilantro, quantity="150", unit_cost="0.01"
    )

    view = await _get(client, restaurant_a, "restock")
    item = view["items"][0]
    assert view["refreshed_at"] is None
    assert item["action"] == "buy_this_week"
    assert item["engine"] == "rules"
    assert item["decision_id"] is None
    assert "Quedan 150 g de Culantro" in item["explanation"]

    refreshed = await _post(client, restaurant_a, "restock/refresh")
    assert refreshed["counts"]["buy_this_week"] == 1
    saved = refreshed["items"][0]
    assert saved["decision_id"] is not None
    assert saved["fallback_reason"] == "not_configured"

    reread = await _get(client, restaurant_a, "restock")
    assert reread["items"][0]["decision_id"] == saved["decision_id"]
    assert reread["refreshed_at"] is not None
    assert reread["items"][0]["is_stale"] is False

    audit = await _get(client, restaurant_a, "ai-decisions", kind="restock")
    assert audit["total"] == 1
    decision = audit["items"][0]
    assert decision["subject_type"] == "ingredient"
    assert decision["subject_id"] == cilantro
    assert decision["subject_label"] == "Culantro"
    assert decision["order_number"] is None
    # Las reglas no tienen una confianza que dar: queda vacía y se dice por qué.
    assert decision["confidence"] is None
    assert decision["confidence_kind"] == "rule"
    assert decision["confidence_kind_label"] == "Regla fija"
    assert decision["input_state"]["stock_on_hand"] == 150.0
    assert decision["output"]["action"] == "buy_this_week"


def _choice(choice: str, confidence: float, options: list[str]) -> dict[str, Any]:
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": {o: (1.0 if o == choice else 0.0) for o in options},
    }


def _with_jev(client: AsyncClient, handler: Any) -> None:
    app = client._transport.app  # type: ignore[attr-defined]
    jev = JevDecisionEngine(
        api_key="clave",
        base_url="https://api.typesafe.test",
        model="jev-latest",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    app.dependency_overrides[get_decision_engine] = lambda: DecisionEngineSelector(
        rules=RuleBasedDecisionEngine(), jev=jev, min_confidence=0.6
    )


async def test_with_jev_jev_decides_and_low_confidence_records_the_fallback(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    arroz = await _ingredient(client, restaurant_a, "Arroz")
    cilantro = await _ingredient(client, restaurant_a, "Culantro", min_stock="200")
    for ingredient in (arroz, cilantro):
        await _move_stock(
            client,
            restaurant_a,
            "purchases",
            ingredient_id=ingredient,
            quantity="150",
            unit_cost="0.01",
        )

    def respond(request: httpx.Request) -> httpx.Response:
        confident = b"Arroz" in request.content
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "action": _choice(
                        "wait",
                        0.92 if confident else 0.3,
                        list(RESTOCK_QUESTIONS["action"]["criteria"]),
                    ),
                    "urgency": {
                        "type": "score",
                        "score": 0.2,
                        "confidence": 0.8,
                        "probabilities": {},
                    },
                },
                "usage": {"input_tokens": 400, "output_tokens": 30},
            },
        )

    _with_jev(client, respond)
    items = {i["name"]: i for i in (await _post(client, restaurant_a, "restock/refresh"))["items"]}

    assert items["Arroz"]["engine"] == "jev"
    assert items["Arroz"]["model"] == "jev-1.13.0"
    assert items["Arroz"]["confidence"] == 0.92
    assert "Jev" in items["Arroz"]["explanation"]
    assert items["Culantro"]["engine"] == "rules"
    assert items["Culantro"]["fallback_reason"] == "low_confidence"
    assert items["Culantro"]["action"] == "buy_this_week"

    audit = await _get(client, restaurant_a, "ai-decisions", engine="rules")
    assert audit["items"][0]["output"]["details"]["jev"]["confidence"] == 0.3
    assert audit["items"][0]["confidence_kind"] == "rule"
    from_jev = (await _get(client, restaurant_a, "ai-decisions", engine="jev"))["items"][0]
    assert from_jev["confidence"] == 0.92
    assert from_jev["confidence_kind"] == "model"
    assert from_jev["confidence_kind_label"] == "Confianza del modelo"


# -- Notas de pedido ---------------------------------------------------------


async def _order_with_notes(
    client: AsyncClient, restaurant: StaffedRestaurant, menu: SeededMenu, send: bool = True
) -> dict[str, Any]:
    waiter = authorization_for(restaurant.waiter)
    created = await client.post(
        ORDERS_URL,
        json={
            "type": "dine_in",
            "table_id": menu.table_1,
            "notes": "Mesa con un niño celíaco",
            "items": [
                {"menu_item_id": menu.lomo, "quantity": 1, "notes": "sin cebolla"},
                {"menu_item_id": menu.aji, "quantity": 1, "notes": "es alérgica al maní"},
                {"menu_item_id": menu.chicha, "quantity": 2},
            ],
        },
        headers=waiter,
    )
    assert created.status_code == 201, created.text
    order = created.json()
    if send:
        sent = await client.post(f"{ORDERS_URL}/{order['id']}/send", headers=waiter)
        assert sent.status_code == 200, sent.text
    return order


async def test_send_to_kitchen_classifies_notes_in_background_and_notifies(
    client: AsyncClient,
    restaurant_a: StaffedRestaurant,
    menu_a: SeededMenu,
    jobs: BackgroundJobs,
    broker: LocalBroker,
) -> None:
    async with broker.subscribe() as notices:
        order = await _order_with_notes(client, restaurant_a, menu_a)
        await jobs.drain()
        topics = []
        while not notices.empty():
            topics.append(notices.get_nowait())

    assert any(a.topic == "insights" and a.reference_id == order["id"] for a in topics)
    notes = await _get(client, restaurant_a, "order-notes", order_ids=[order["id"]])
    by_note = {n["note"]: n for n in notes["items"]}
    assert set(by_note) == {"Mesa con un niño celíaco", "sin cebolla", "es alérgica al maní"}
    allergy = by_note["es alérgica al maní"]
    assert allergy["status"] == "classified"
    assert allergy["mentions_allergy"] is True
    assert allergy["note_type"] == "allergy"
    assert allergy["order_item_id"] == order["items"][1]["id"]
    assert allergy["engine"] == "rules"
    assert by_note["sin cebolla"]["mentions_allergy"] is False
    assert by_note["sin cebolla"]["note_type"] == "preference"
    assert by_note["Mesa con un niño celíaco"]["scope"] == "order"
    assert by_note["Mesa con un niño celíaco"]["mentions_allergy"] is True


async def test_kitchen_notice_is_idempotent(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, jobs: BackgroundJobs
) -> None:
    order = await _order_with_notes(client, restaurant_a, menu_a)
    await jobs.drain()
    # Platos nuevos con el pedido en cocina: vuelve a avisar, con todo el pedido.
    added = await client.post(
        f"{ORDERS_URL}/{order['id']}/items",
        json={"items": [{"menu_item_id": menu_a.chicha, "quantity": 1, "notes": "sin hielo"}]},
        headers=authorization_for(restaurant_a.waiter),
    )
    assert added.status_code == 200, added.text
    await jobs.drain()

    audit = await _get(client, restaurant_a, "ai-decisions", kind="order_note")
    # Tres notas del envío más la nueva; ninguna se clasificó dos veces.
    assert audit["total"] == 4
    subjects = {(d["subject_type"], d["subject_label"]) for d in audit["items"]}
    assert subjects == {
        ("order", None),
        ("order_item", "Lomo saltado"),
        ("order_item", "Ají de gallina"),
        ("order_item", "Chicha morada"),
    }
    assert {d["order_number"] for d in audit["items"]} == {order["number"]}
    assert (await _post(client, restaurant_a, "order-notes/classify"))["classified"] == 0


class _HeldEngine:
    """Un motor que no responde hasta que la prueba lo suelta, como Jev lento."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def classify_note(self, note: KitchenNote) -> Verdict[NoteOutcome]:
        self.calls += 1
        await self.release.wait()
        return Verdict(
            outcome=NoteOutcome(mentions_allergy=True, note_type=NoteType.ALLERGY),
            engine=Engine.JEV,
            model="jev-1.13.0",
            confidence=0.99,
        )


async def test_send_to_kitchen_does_not_wait_for_the_ai(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, jobs: BackgroundJobs
) -> None:
    slow = _HeldEngine()
    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_decision_engine] = lambda: slow

    order = await _order_with_notes(client, restaurant_a, menu_a)

    # La respuesta llegó y el pedido ya está en cocina mientras la IA piensa.
    assert jobs.pending == 1
    in_kitchen = await client.get(
        f"{ORDERS_URL}/{order['id']}", headers=authorization_for(restaurant_a.admin)
    )
    assert in_kitchen.json()["status"] == "in_kitchen"
    pending = await _get(client, restaurant_a, "order-notes", order_ids=[order["id"]])
    assert {n["status"] for n in pending["items"]} == {"pending"}

    slow.release.set()
    await jobs.drain()
    ready = await _get(client, restaurant_a, "order-notes", order_ids=[order["id"]])
    assert {n["engine"] for n in ready["items"]} == {"jev"}
    assert slow.calls == 3


async def test_rejected_send_classifies_nothing(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu, jobs: BackgroundJobs
) -> None:
    order = await _order_with_notes(client, restaurant_a, menu_a)
    await jobs.drain()
    # Enviar dos veces es una transición inválida: la petición se deshace.
    repeated = await client.post(
        f"{ORDERS_URL}/{order['id']}/send", headers=authorization_for(restaurant_a.waiter)
    )

    assert repeated.status_code == 409
    assert jobs.pending == 0


async def test_with_external_ai_off_rules_decide_and_nothing_leaves(
    app: FastAPI, client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    # El motor real, no el de las pruebas: es el que mira la preferencia del local.
    app.dependency_overrides.pop(get_decision_engine)
    disabled = await client.patch(
        url("restaurant"),
        json={"external_ai_enabled": False},
        headers=authorization_for(restaurant_a.admin),
    )
    await _order_with_notes(client, restaurant_a, menu_a, send=False)

    run = await _post(client, restaurant_a, "order-notes/classify")
    audit = await _get(client, restaurant_a, "ai-decisions", kind="order_note")

    assert disabled.json()["external_ai_enabled"] is False
    assert run["classified"] == 3
    assert {d["fallback_reason"] for d in audit["items"]} == {"disabled"}
    assert {d["engine"] for d in audit["items"]} == {"rules"}


async def test_saved_note_has_no_personal_data_and_is_reused(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    await client.post(
        ORDERS_URL,
        json={
            "type": "takeaway",
            "customer_name": "Rosa Quispe",
            "notes": "Rosa es alérgica al maní, llamar al 912345678",
            "items": [{"menu_item_id": menu_a.lomo, "quantity": 1}],
        },
        headers=authorization_for(restaurant_a.waiter),
    )

    first_run = await _post(client, restaurant_a, "order-notes/classify")
    second_run = await _post(client, restaurant_a, "order-notes/classify")
    audit = await _get(client, restaurant_a, "ai-decisions", kind="order_note")

    assert first_run["classified"] == 1
    # La nota no cambió: la clasificación guardada sigue valiendo.
    assert second_run["classified"] == 0
    saved = audit["items"][0]["input_state"]["note"]
    assert "Rosa" not in saved
    assert "912345678" not in saved
    assert "maní" in saved


async def test_owner_classifies_notes_of_open_orders_by_hand(
    client: AsyncClient, restaurant_a: StaffedRestaurant, menu_a: SeededMenu
) -> None:
    order = await _order_with_notes(client, restaurant_a, menu_a, send=False)

    run = await _post(client, restaurant_a, "order-notes/classify")

    assert run["classified"] == 3
    assert run["allergies"] == 2
    assert {n["order_id"] for n in run["items"]} == {order["id"]}


# -- Permisos y aislamiento --------------------------------------------------

READS = [
    "summary",
    "sales/daily",
    "sales/hourly",
    "payments",
    "waiters",
    "dishes/top",
    "dishes/margins",
    "low-stock",
    "waste",
    "restock",
    "ai-decisions",
    "order-notes?order_ids=1",
]
ACTIONS = ["restock/refresh", "order-notes/classify", "waste/classify"]


@pytest.mark.parametrize("path", READS)
async def test_waiter_cannot_see_insights(
    client: AsyncClient, restaurant_a: StaffedRestaurant, path: str
) -> None:
    response = await client.get(
        f"{INSIGHTS_URL}/{path}", headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 403


@pytest.mark.parametrize("path", ACTIONS)
async def test_waiter_cannot_trigger_decisions(
    client: AsyncClient, restaurant_a: StaffedRestaurant, path: str
) -> None:
    response = await client.post(
        f"{INSIGHTS_URL}/{path}", headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 403


async def test_no_credentials_responds_401(client: AsyncClient) -> None:
    assert (await client.get(f"{INSIGHTS_URL}/summary")).status_code == 401


@pytest.mark.usefixtures("sales_a")
async def test_a_restaurant_sees_nothing_of_the_other(
    client: AsyncClient,
    session: AsyncSession,
    restaurant_a: StaffedRestaurant,
    restaurant_b: StaffedRestaurant,
    menu_a: SeededMenu,
    jobs: BackgroundJobs,
) -> None:
    await seed_menu(session, restaurant_b.id)
    await _ingredient(client, restaurant_a, "Culantro", min_stock="200")
    order_a = await _order_with_notes(client, restaurant_a, menu_a)
    await jobs.drain()
    await _post(client, restaurant_a, "restock/refresh")

    summary_b = await _get(client, restaurant_b, "summary", **WEEK)
    assert summary_b["sales"] == "0.00"
    assert summary_b["cancelled_orders"] == 0
    assert (await _get(client, restaurant_b, "payments", **WEEK))["methods"] == []
    assert (await _get(client, restaurant_b, "waiters", **WEEK))["waiters"] == []
    assert (await _get(client, restaurant_b, "dishes/top", **WEEK))["dishes"] == []
    assert (await _get(client, restaurant_b, "low-stock")) == []
    assert (await _get(client, restaurant_b, "restock"))["items"] == []
    assert (await _get(client, restaurant_b, "ai-decisions"))["total"] == 0
    foreign_notes = await _get(client, restaurant_b, "order-notes", order_ids=[order_a["id"]])
    assert foreign_notes["items"] == []
    assert (await _post(client, restaurant_b, "order-notes/classify"))["classified"] == 0
