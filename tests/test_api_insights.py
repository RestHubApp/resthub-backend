"""Indicadores por HTTP: cuentas, zona horaria, decisiones, permisos y aislamiento."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
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
from tests.builders import Carta, carta
from tests.conftest import StaffedRestaurant, authorization_for

INSIGHTS_URL = "/api/v1/insights"
ORDERS_URL = "/api/v1/orders"
INVENTORY_URL = "/api/v1/inventory"
SEMANA = {"date_from": "2026-09-21", "date_to": "2026-09-27"}


async def _venta(
    session: AsyncSession,
    local: StaffedRestaurant,
    menu: Carta,
    number: int,
    dia: date,
    abierto_utc: datetime,
    *platos: tuple[int, str, str, int],
    estado: OrderStatus = OrderStatus.PAID,
    medio: PaymentMethod | None = PaymentMethod.CASH,
    mesero: int | None = None,
) -> Order:
    """Un pedido ya cerrado, escrito directo en la base con la hora que se quiera."""
    items = [
        OrderItem(menu_item_id=plato, name=nombre, unit_price=Decimal(precio), quantity=q)
        for plato, nombre, precio, q in platos
    ]
    waiter_id = mesero or local.waiter.id or 0
    total = sum((item.subtotal for item in items), Decimal("0.00"))
    order = Order(
        restaurant_id=local.id,
        number=number,
        business_date=dia,
        type=OrderType.TAKEAWAY,
        waiter_id=waiter_id,
        status=estado,
        items=items,
        payments=(
            [Payment(method=medio, amount=total, received_by=waiter_id, created_at=abierto_utc)]
            if estado is OrderStatus.PAID and medio is not None
            else []
        ),
        cancel_reason="El cliente se fue" if estado is OrderStatus.CANCELLED else "",
        created_at=abierto_utc,
        updated_at=abierto_utc,
        paid_at=abierto_utc if estado is OrderStatus.PAID else None,
    )
    saved = await SqlAlchemyOrderRepository(session).add(order)
    await session.commit()
    return saved


@pytest.fixture
async def menu_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    return await carta(session, local_a.id)


@pytest.fixture
async def ventas_a(session: AsyncSession, local_a: StaffedRestaurant, menu_a: Carta) -> None:
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
    await _venta(
        session, local_a, menu_a, 1, date(2026, 9, 21), datetime(2026, 9, 21, 17, 30, tzinfo=UTC),
        (*lomo, 2),
    )  # fmt: skip
    await _venta(
        session, local_a, menu_a, 2, date(2026, 9, 21), datetime(2026, 9, 22, 4, 30, tzinfo=UTC),
        (*aji, 1), medio=PaymentMethod.YAPE,
    )  # fmt: skip
    await _venta(
        session, local_a, menu_a, 1, date(2026, 9, 23), datetime(2026, 9, 23, 20, 0, tzinfo=UTC),
        (*lomo, 1), (*chicha, 2), medio=PaymentMethod.YAPE, mesero=local_a.admin.id,
    )  # fmt: skip
    await _venta(
        session, local_a, menu_a, 2, date(2026, 9, 23), datetime(2026, 9, 23, 21, 0, tzinfo=UTC),
        (*lomo, 1), estado=OrderStatus.CANCELLED,
    )  # fmt: skip
    await _venta(
        session, local_a, menu_a, 1, date(2026, 9, 20), datetime(2026, 9, 20, 18, 0, tzinfo=UTC),
        (*lomo, 1),
    )  # fmt: skip


async def _get(
    client: AsyncClient, local: StaffedRestaurant, path: str, **params: Any
) -> dict[str, Any]:
    response = await client.get(
        f"{INSIGHTS_URL}/{path}", params=params, headers=authorization_for(local.admin)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _post(client: AsyncClient, local: StaffedRestaurant, path: str) -> dict[str, Any]:
    response = await client.post(f"{INSIGHTS_URL}/{path}", headers=authorization_for(local.admin))
    assert response.status_code == 200, response.text
    return response.json()


# -- Ventas ------------------------------------------------------------------


@pytest.mark.usefixtures("ventas_a")
async def test_resumen_con_ticket_promedio_cancelados_y_periodo_anterior(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    resumen = await _get(client, local_a, "summary", **SEMANA)

    assert resumen["period"] == {
        "date_from": "2026-09-21",
        "date_to": "2026-09-27",
        "days": 7,
        "timezone": "America/Lima",
    }
    assert resumen["sales"] == "117.00"
    assert resumen["paid_orders"] == 3
    assert resumen["average_ticket"] == "39.00"
    assert resumen["cancelled_orders"] == 1
    assert resumen["cancelled_amount"] == "28.00"
    assert resumen["previous"]["date_from"] == "2026-09-14"
    assert resumen["previous"]["sales"] == "28.00"
    # 117 contra 28: +317.9 %.
    assert resumen["sales_change_percent"] == "317.9"


@pytest.mark.usefixtures("ventas_a")
async def test_ventas_por_dia_cuentan_el_dia_del_local(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    dias = (await _get(client, local_a, "sales/daily", **SEMANA))["days"]

    assert len(dias) == 7
    assert dias[0] == {
        "date": "2026-09-21",
        "sales": "78.00",
        "paid_orders": 2,
        "average_ticket": "39.00",
    }
    assert dias[1]["sales"] == "0.00"
    assert dias[2]["sales"] == "39.00"


@pytest.mark.usefixtures("ventas_a")
async def test_el_mapa_de_calor_ubica_la_venta_de_medianoche_en_su_hora_local(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    datos = await _get(client, local_a, "sales/hourly", **SEMANA)
    celdas = {(c["weekday"], c["hour"]): c for c in datos["cells"]}

    assert len(celdas) == 168
    assert celdas[(0, 12)]["sales"] == "56.00"
    assert celdas[(0, 23)]["sales"] == "22.00"
    assert celdas[(1, 4)]["sales"] == "0.00"
    assert celdas[(2, 15)]["sales"] == "39.00"
    assert datos["peak"]["weekday"] == 0 and datos["peak"]["hour"] == 12


@pytest.mark.usefixtures("ventas_a")
async def test_platos_mas_vendidos_y_medios_de_pago(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    platos = (await _get(client, local_a, "dishes/top", limit=2, **SEMANA))["dishes"]
    pagos = await _get(client, local_a, "payments", **SEMANA)

    # Lo cancelado no cuenta como vendido.
    assert [(p["name"], p["quantity"], p["revenue"]) for p in platos] == [
        ("Lomo saltado", 3, "84.00"),
        ("Chicha morada", 2, "11.00"),
    ]
    assert pagos["total"] == "117.00"
    assert [(m["method"], m["amount"], m["share_percent"]) for m in pagos["methods"]] == [
        ("yape", "61.00", "52.1"),
        ("cash", "56.00", "47.9"),
    ]


@pytest.mark.usefixtures("ventas_a")
async def test_rendimiento_por_mesero(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    meseros = (await _get(client, local_a, "waiters", **SEMANA))["waiters"]

    assert [(m["name"], m["paid_orders"], m["sales"], m["cancelled_orders"]) for m in meseros] == [
        ("Luis Torres", 2, "78.00", 1),
        ("Rosa Pérez", 1, "39.00", 0),
    ]


@pytest.mark.usefixtures("ventas_a")
async def test_margen_por_plato_con_el_costo_de_la_receta(
    client: AsyncClient, local_a: StaffedRestaurant, menu_a: Carta
) -> None:
    admin = authorization_for(local_a.admin)
    carne = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": "Lomo de res", "unit": "g", "unit_cost": "0.042"},
        headers=admin,
    )
    await client.put(
        f"{INVENTORY_URL}/recipes/{menu_a.lomo}",
        json={"lines": [{"ingredient_id": carne.json()["id"], "quantity": "200"}]},
        headers=admin,
    )

    platos = {
        p["name"]: p for p in (await _get(client, local_a, "dishes/margins", **SEMANA))["dishes"]
    }

    lomo = platos["Lomo saltado"]
    assert lomo["recipe_cost"] == "8.40"
    assert lomo["unit_margin"] == "19.60"
    assert lomo["margin_percent"] == "70.0"
    assert lomo["quantity_sold"] == 3
    assert lomo["gross_margin"] == "58.80"
    assert platos["Ají de gallina"]["recipe_cost"] is None
    # Los platos retirados que no se vendieron no aparecen.
    assert "Seco de res" not in platos


async def test_un_rango_invalido_responde_422(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.get(
        f"{INSIGHTS_URL}/summary",
        params={"date_from": "2026-09-10", "date_to": "2026-09-01"},
        headers=authorization_for(local_a.admin),
    )

    assert response.status_code == 422
    assert "posterior" in response.json()["detail"]


# -- Almacén -----------------------------------------------------------------


async def _insumo(client: AsyncClient, local: StaffedRestaurant, nombre: str, **extra: str) -> int:
    response = await client.post(
        f"{INVENTORY_URL}/ingredients",
        json={"name": nombre, "unit": "g", **extra},
        headers=authorization_for(local.admin),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _mover(client: AsyncClient, local: StaffedRestaurant, tipo: str, **body: Any) -> None:
    response = await client.post(
        f"{INVENTORY_URL}/{tipo}", json=body, headers=authorization_for(local.admin)
    )
    assert response.status_code == 201, response.text


async def test_mermas_por_insumo_y_por_causa_tras_clasificarlas(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    queso = await _insumo(client, local_a, "Queso fresco")
    await _mover(
        client, local_a, "purchases", ingredient_id=queso, quantity="2000", unit_cost="0.02"
    )
    await _mover(client, local_a, "waste", ingredient_id=queso, quantity="300", reason="Se venció")
    await _mover(
        client, local_a, "waste", ingredient_id=queso, quantity="200", reason="Se cayó al piso"
    )

    antes = await _get(client, local_a, "waste")
    assert antes["events"] == 2
    assert antes["total_cost"] == "10.00"
    assert antes["pending_classification"] == 2
    assert antes["by_ingredient"][0]["quantity"] == "500.000"

    corrida = await _post(client, local_a, "waste/classify")
    assert corrida["classified"] == 2
    assert {c["cause"] for c in corrida["by_cause"]} == {"expiration", "mishandling"}
    # Ya clasificadas, una segunda corrida no hace nada.
    assert (await _post(client, local_a, "waste/classify"))["classified"] == 0

    despues = await _get(client, local_a, "waste")
    assert despues["pending_classification"] == 0
    assert {(c["cause"], c["cost"]) for c in despues["by_cause"]} == {
        ("expiration", "6.00"),
        ("mishandling", "4.00"),
    }
    auditoria = await _get(client, local_a, "ai-decisions", kind="waste_cause")
    assert {d["subject_type"] for d in auditoria["items"]} == {"stock_movement"}
    assert {d["subject_label"] for d in auditoria["items"]} == {"Queso fresco"}
    assert {d["order_number"] for d in auditoria["items"]} == {None}


async def test_insumos_bajo_el_minimo(client: AsyncClient, local_a: StaffedRestaurant) -> None:
    culantro = await _insumo(client, local_a, "Culantro", min_stock="200")
    await _insumo(client, local_a, "Arroz", min_stock="0")
    await _mover(
        client, local_a, "purchases", ingredient_id=culantro, quantity="150", unit_cost="0.01"
    )

    bajos = await _get(client, local_a, "low-stock")

    assert bajos == [
        {
            "ingredient_id": culantro,
            "name": "Culantro",
            "unit": "g",
            "stock": "150.000",
            "min_stock": "200.000",
            "missing": "50.000",
        }
    ]


# -- Reposición --------------------------------------------------------------


async def test_reposicion_sin_decisiones_muestra_las_reglas_y_al_actualizar_las_guarda(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    culantro = await _insumo(client, local_a, "Culantro", min_stock="200")
    await _mover(
        client, local_a, "purchases", ingredient_id=culantro, quantity="150", unit_cost="0.01"
    )

    vista = await _get(client, local_a, "restock")
    item = vista["items"][0]
    assert vista["refreshed_at"] is None
    assert item["action"] == "buy_this_week"
    assert item["engine"] == "rules"
    assert item["decision_id"] is None
    assert "Quedan 150 g de Culantro" in item["explanation"]

    actualizada = await _post(client, local_a, "restock/refresh")
    assert actualizada["counts"]["buy_this_week"] == 1
    guardada = actualizada["items"][0]
    assert guardada["decision_id"] is not None
    assert guardada["fallback_reason"] == "not_configured"

    releida = await _get(client, local_a, "restock")
    assert releida["items"][0]["decision_id"] == guardada["decision_id"]
    assert releida["refreshed_at"] is not None
    assert releida["items"][0]["is_stale"] is False

    auditoria = await _get(client, local_a, "ai-decisions", kind="restock")
    assert auditoria["total"] == 1
    decision = auditoria["items"][0]
    assert decision["subject_type"] == "ingredient"
    assert decision["subject_id"] == culantro
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


def _con_jev(client: AsyncClient, handler: Any) -> None:
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


async def test_con_jev_decide_jev_y_con_poca_confianza_queda_registrado_el_respaldo(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    arroz = await _insumo(client, local_a, "Arroz")
    culantro = await _insumo(client, local_a, "Culantro", min_stock="200")
    for insumo in (arroz, culantro):
        await _mover(
            client, local_a, "purchases", ingredient_id=insumo, quantity="150", unit_cost="0.01"
        )

    def responder(request: httpx.Request) -> httpx.Response:
        seguro = b"Arroz" in request.content
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "action": _choice(
                        "wait",
                        0.92 if seguro else 0.3,
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

    _con_jev(client, responder)
    items = {i["name"]: i for i in (await _post(client, local_a, "restock/refresh"))["items"]}

    assert items["Arroz"]["engine"] == "jev"
    assert items["Arroz"]["model"] == "jev-1.13.0"
    assert items["Arroz"]["confidence"] == 0.92
    assert "Jev" in items["Arroz"]["explanation"]
    assert items["Culantro"]["engine"] == "rules"
    assert items["Culantro"]["fallback_reason"] == "low_confidence"
    assert items["Culantro"]["action"] == "buy_this_week"

    auditoria = await _get(client, local_a, "ai-decisions", engine="rules")
    assert auditoria["items"][0]["output"]["details"]["jev"]["confidence"] == 0.3
    assert auditoria["items"][0]["confidence_kind"] == "rule"
    de_jev = (await _get(client, local_a, "ai-decisions", engine="jev"))["items"][0]
    assert de_jev["confidence"] == 0.92
    assert de_jev["confidence_kind"] == "model"
    assert de_jev["confidence_kind_label"] == "Confianza del modelo"


# -- Notas de pedido ---------------------------------------------------------


async def _pedido_con_notas(
    client: AsyncClient, local: StaffedRestaurant, menu: Carta, enviar: bool = True
) -> dict[str, Any]:
    mesero = authorization_for(local.waiter)
    creado = await client.post(
        ORDERS_URL,
        json={
            "type": "dine_in",
            "table_id": menu.mesa_1,
            "notes": "Mesa con un niño celíaco",
            "items": [
                {"menu_item_id": menu.lomo, "quantity": 1, "notes": "sin cebolla"},
                {"menu_item_id": menu.aji, "quantity": 1, "notes": "es alérgica al maní"},
                {"menu_item_id": menu.chicha, "quantity": 2},
            ],
        },
        headers=mesero,
    )
    assert creado.status_code == 201, creado.text
    pedido = creado.json()
    if enviar:
        enviado = await client.post(f"{ORDERS_URL}/{pedido['id']}/send", headers=mesero)
        assert enviado.status_code == 200, enviado.text
    return pedido


async def test_enviar_a_cocina_clasifica_las_notas_en_segundo_plano_y_avisa(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    menu_a: Carta,
    jobs: BackgroundJobs,
    broker: LocalBroker,
) -> None:
    async with broker.subscribe() as avisos:
        pedido = await _pedido_con_notas(client, local_a, menu_a)
        await jobs.drain()
        temas = []
        while not avisos.empty():
            temas.append(avisos.get_nowait())

    assert any(a.topic == "insights" and a.reference_id == pedido["id"] for a in temas)
    notas = await _get(client, local_a, "order-notes", order_ids=[pedido["id"]])
    por_nota = {n["note"]: n for n in notas["items"]}
    assert set(por_nota) == {"Mesa con un niño celíaco", "sin cebolla", "es alérgica al maní"}
    alergia = por_nota["es alérgica al maní"]
    assert alergia["status"] == "classified"
    assert alergia["mentions_allergy"] is True
    assert alergia["note_type"] == "allergy"
    assert alergia["order_item_id"] == pedido["items"][1]["id"]
    assert alergia["engine"] == "rules"
    assert por_nota["sin cebolla"]["mentions_allergy"] is False
    assert por_nota["sin cebolla"]["note_type"] == "preference"
    assert por_nota["Mesa con un niño celíaco"]["scope"] == "order"
    assert por_nota["Mesa con un niño celíaco"]["mentions_allergy"] is True


async def test_el_aviso_de_cocina_es_idempotente(
    client: AsyncClient, local_a: StaffedRestaurant, menu_a: Carta, jobs: BackgroundJobs
) -> None:
    pedido = await _pedido_con_notas(client, local_a, menu_a)
    await jobs.drain()
    # Platos nuevos con el pedido en cocina: vuelve a avisar, con todo el pedido.
    agregado = await client.post(
        f"{ORDERS_URL}/{pedido['id']}/items",
        json={"items": [{"menu_item_id": menu_a.chicha, "quantity": 1, "notes": "sin hielo"}]},
        headers=authorization_for(local_a.waiter),
    )
    assert agregado.status_code == 200, agregado.text
    await jobs.drain()

    auditoria = await _get(client, local_a, "ai-decisions", kind="order_note")
    # Tres notas del envío más la nueva; ninguna se clasificó dos veces.
    assert auditoria["total"] == 4
    asuntos = {(d["subject_type"], d["subject_label"]) for d in auditoria["items"]}
    assert asuntos == {
        ("order", None),
        ("order_item", "Lomo saltado"),
        ("order_item", "Ají de gallina"),
        ("order_item", "Chicha morada"),
    }
    assert {d["order_number"] for d in auditoria["items"]} == {pedido["number"]}
    assert (await _post(client, local_a, "order-notes/classify"))["classified"] == 0


class _Retenido:
    """Un motor que no responde hasta que la prueba lo suelta, como Jev lento."""

    def __init__(self) -> None:
        self.soltar = asyncio.Event()
        self.preguntas = 0

    async def classify_note(self, note: KitchenNote) -> Verdict[NoteOutcome]:
        self.preguntas += 1
        await self.soltar.wait()
        return Verdict(
            outcome=NoteOutcome(mentions_allergy=True, note_type=NoteType.ALLERGY),
            engine=Engine.JEV,
            model="jev-1.13.0",
            confidence=0.99,
        )


async def test_enviar_a_cocina_no_espera_a_la_ia(
    client: AsyncClient, local_a: StaffedRestaurant, menu_a: Carta, jobs: BackgroundJobs
) -> None:
    lento = _Retenido()
    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_decision_engine] = lambda: lento

    pedido = await _pedido_con_notas(client, local_a, menu_a)

    # La respuesta llegó y el pedido ya está en cocina mientras la IA piensa.
    assert jobs.pending == 1
    en_cocina = await client.get(
        f"{ORDERS_URL}/{pedido['id']}", headers=authorization_for(local_a.admin)
    )
    assert en_cocina.json()["status"] == "in_kitchen"
    pendientes = await _get(client, local_a, "order-notes", order_ids=[pedido["id"]])
    assert {n["status"] for n in pendientes["items"]} == {"pending"}

    lento.soltar.set()
    await jobs.drain()
    listas = await _get(client, local_a, "order-notes", order_ids=[pedido["id"]])
    assert {n["engine"] for n in listas["items"]} == {"jev"}
    assert lento.preguntas == 3


async def test_un_envio_rechazado_no_clasifica_nada(
    client: AsyncClient, local_a: StaffedRestaurant, menu_a: Carta, jobs: BackgroundJobs
) -> None:
    pedido = await _pedido_con_notas(client, local_a, menu_a)
    await jobs.drain()
    # Enviar dos veces es una transición inválida: la petición se deshace.
    repetido = await client.post(
        f"{ORDERS_URL}/{pedido['id']}/send", headers=authorization_for(local_a.waiter)
    )

    assert repetido.status_code == 409
    assert jobs.pending == 0


async def test_el_encargado_clasifica_a_mano_las_notas_de_los_pedidos_en_curso(
    client: AsyncClient, local_a: StaffedRestaurant, menu_a: Carta
) -> None:
    pedido = await _pedido_con_notas(client, local_a, menu_a, enviar=False)

    corrida = await _post(client, local_a, "order-notes/classify")

    assert corrida["classified"] == 3
    assert corrida["allergies"] == 2
    assert {n["order_id"] for n in corrida["items"]} == {pedido["id"]}


# -- Permisos y aislamiento --------------------------------------------------

LECTURAS = [
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
ACCIONES = ["restock/refresh", "order-notes/classify", "waste/classify"]


@pytest.mark.parametrize("path", LECTURAS)
async def test_el_mesero_no_ve_indicadores(
    client: AsyncClient, local_a: StaffedRestaurant, path: str
) -> None:
    response = await client.get(f"{INSIGHTS_URL}/{path}", headers=authorization_for(local_a.waiter))

    assert response.status_code == 403


@pytest.mark.parametrize("path", ACCIONES)
async def test_el_mesero_no_dispara_decisiones(
    client: AsyncClient, local_a: StaffedRestaurant, path: str
) -> None:
    response = await client.post(
        f"{INSIGHTS_URL}/{path}", headers=authorization_for(local_a.waiter)
    )

    assert response.status_code == 403


async def test_sin_credencial_responde_401(client: AsyncClient) -> None:
    assert (await client.get(f"{INSIGHTS_URL}/summary")).status_code == 401


@pytest.mark.usefixtures("ventas_a")
async def test_un_restaurante_no_ve_nada_del_otro(
    client: AsyncClient,
    session: AsyncSession,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    menu_a: Carta,
    jobs: BackgroundJobs,
) -> None:
    await carta(session, local_b.id)
    await _insumo(client, local_a, "Culantro", min_stock="200")
    pedido_a = await _pedido_con_notas(client, local_a, menu_a)
    await jobs.drain()
    await _post(client, local_a, "restock/refresh")

    resumen_b = await _get(client, local_b, "summary", **SEMANA)
    assert resumen_b["sales"] == "0.00" and resumen_b["cancelled_orders"] == 0
    assert (await _get(client, local_b, "payments", **SEMANA))["methods"] == []
    assert (await _get(client, local_b, "waiters", **SEMANA))["waiters"] == []
    assert (await _get(client, local_b, "dishes/top", **SEMANA))["dishes"] == []
    assert (await _get(client, local_b, "low-stock")) == []
    assert (await _get(client, local_b, "restock"))["items"] == []
    assert (await _get(client, local_b, "ai-decisions"))["total"] == 0
    notas_ajenas = await _get(client, local_b, "order-notes", order_ids=[pedido_a["id"]])
    assert notas_ajenas["items"] == []
    assert (await _post(client, local_b, "order-notes/classify"))["classified"] == 0
