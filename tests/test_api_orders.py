"""Pedidos por HTTP: reglas, permisos del mesero y del encargado, aislamiento."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.identity import Role
from resthub.core.realtime_broker import LocalBroker
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from tests.builders import Carta, carta
from tests.conftest import StaffedRestaurant, authorization_for, build_user

ORDERS_URL = "/api/v1/orders"


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    return await carta(session, local_a.id)


@pytest.fixture
async def carta_b(session: AsyncSession, local_b: StaffedRestaurant) -> Carta:
    return await carta(session, local_b.id)


async def _open(
    client: AsyncClient,
    headers: dict[str, str],
    table_id: int | None,
    *items: tuple[int, int],
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "dine_in" if table_id else "takeaway",
        "table_id": table_id,
        "items": [{"menu_item_id": dish, "quantity": quantity} for dish, quantity in items],
        **extra,
    }
    response = await client.post(ORDERS_URL, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _step(
    client: AsyncClient, headers: dict[str, str], order_id: int, action: str, **body: Any
) -> dict[str, Any]:
    response = await client.post(
        f"{ORDERS_URL}/{order_id}/{action}", json=body or None, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _moment(value: str) -> datetime:
    # Se comparan como fechas y no como texto: sin microsegundos, el texto
    # ISO pierde la parte decimal y el orden alfabético deja de ser el real.
    return datetime.fromisoformat(value)


async def _served_order(
    client: AsyncClient, local: StaffedRestaurant, menu: Carta
) -> dict[str, Any]:
    mesero, encargado = authorization_for(local.waiter), authorization_for(local.admin)
    order = await _open(client, mesero, menu.mesa_1, (menu.lomo, 2), (menu.chicha, 1))
    await _step(client, mesero, order["id"], "send")
    await _step(client, encargado, order["id"], "ready")
    return await _step(client, mesero, order["id"], "served")


async def test_el_mesero_abre_un_pedido_con_foto_del_menu(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    order = await _open(
        client,
        authorization_for(local_a.waiter),
        carta_a.mesa_1,
        (carta_a.lomo, 2),
        (carta_a.chicha, 1),
        notes="Cumpleaños",
    )

    assert order["status"] == "open"
    assert order["status_label"] == "Abierto"
    assert order["number"] == 1
    assert order["table_label"] == "1"
    assert order["waiter_id"] == local_a.waiter.id
    assert order["waiter_name"] == "Luis Torres"
    assert order["total"] == "61.50"
    assert [(i["name"], i["unit_price"], i["subtotal"]) for i in order["items"]] == [
        ("Lomo saltado", "28.00", "56.00"),
        ("Chicha morada", "5.50", "5.50"),
    ]


async def test_el_numero_es_correlativo_por_restaurante(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    carta_a: Carta,
    carta_b: Carta,
) -> None:
    mesero_a, mesero_b = authorization_for(local_a.waiter), authorization_for(local_b.waiter)

    primero = await _open(client, mesero_a, carta_a.mesa_1, (carta_a.lomo, 1))
    segundo = await _open(client, mesero_a, None, (carta_a.aji, 1), customer_name="Rosa")
    otro_local = await _open(client, mesero_b, carta_b.mesa_1, (carta_b.lomo, 1))

    assert (primero["number"], segundo["number"], otro_local["number"]) == (1, 2, 1)
    assert segundo["type"] == "takeaway"
    assert segundo["table_id"] is None
    assert segundo["customer_name"] == "Rosa"


async def test_una_mesa_solo_tiene_un_pedido_activo(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero = authorization_for(local_a.waiter)
    primero = await _open(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))

    ocupada = await client.post(
        ORDERS_URL, json={"type": "dine_in", "table_id": carta_a.mesa_1}, headers=mesero
    )
    assert ocupada.status_code == 409

    await client.post(
        f"{ORDERS_URL}/{primero['id']}/cancel",
        json={"reason": "Se equivocó de mesa"},
        headers=authorization_for(local_a.admin),
    )
    liberada = await client.post(
        ORDERS_URL, json={"type": "dine_in", "table_id": carta_a.mesa_1}, headers=mesero
    )
    assert liberada.status_code == 201


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"type": "dine_in"}, 422),
        ({"type": "takeaway", "table_id": "{mesa}"}, 422),
    ],
)
async def test_mesa_y_tipo_tienen_que_cuadrar(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    carta_a: Carta,
    body: dict[str, Any],
    expected: int,
) -> None:
    if body.get("table_id") == "{mesa}":
        body = {**body, "table_id": carta_a.mesa_1}

    response = await client.post(ORDERS_URL, json=body, headers=authorization_for(local_a.waiter))

    assert response.status_code == expected


async def test_solo_platos_activos_y_disponibles(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta, carta_b: Carta
) -> None:
    mesero = authorization_for(local_a.waiter)

    async def pedir(dish: int) -> int:
        response = await client.post(
            ORDERS_URL,
            json={"type": "takeaway", "items": [{"menu_item_id": dish}]},
            headers=mesero,
        )
        return response.status_code

    assert await pedir(carta_a.agotado) == 409
    assert await pedir(carta_a.retirado) == 409
    # Un plato de otro local es, para este, un plato que no existe.
    assert await pedir(carta_b.lomo) == 404


async def test_el_recorrido_completo_con_cobro_en_efectivo(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    served = await _served_order(client, local_a, carta_a)
    assert served["status"] == "served"

    paid = await _step(
        client,
        authorization_for(local_a.admin),
        served["id"],
        "charge",
        payment_method="cash",
        amount_received="100",
    )

    assert paid["status"] == "paid"
    assert paid["total"] == "61.50"
    assert paid["amount_received"] == "100.00"
    assert paid["change"] == "38.50"
    assert paid["payment_method_label"] == "Efectivo"
    assert paid["paid_at"] is not None
    mesas = await client.get("/api/v1/tables", headers=authorization_for(local_a.waiter))
    assert mesas.json()[0]["status"] == "free"


async def test_el_efectivo_insuficiente_y_el_monto_en_yape_se_rechazan(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    served = await _served_order(client, local_a, carta_a)
    encargado = authorization_for(local_a.admin)
    url = f"{ORDERS_URL}/{served['id']}/charge"

    corto = await client.post(
        url, json={"payment_method": "cash", "amount_received": "50.00"}, headers=encargado
    )
    yape = await client.post(
        url, json={"payment_method": "yape", "amount_received": "61.50"}, headers=encargado
    )
    bien = await client.post(url, json={"payment_method": "yape"}, headers=encargado)

    assert corto.status_code == 422
    assert yape.status_code == 422
    assert bien.status_code == 200
    assert bien.json()["change"] is None


async def test_agregar_platos_a_lo_servido_lo_devuelve_a_cocina(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    served = await _served_order(client, local_a, carta_a)

    response = await client.post(
        f"{ORDERS_URL}/{served['id']}/items",
        json={"items": [{"menu_item_id": carta_a.aji, "quantity": 1, "notes": "sin ají"}]},
        headers=authorization_for(local_a.waiter),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "in_kitchen"
    assert response.json()["total"] == "83.50"
    assert response.json()["items"][-1]["notes"] == "sin ají"


async def test_editar_y_quitar_solo_con_el_pedido_abierto(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero = authorization_for(local_a.waiter)
    order = await _open(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1), (carta_a.chicha, 1))
    lomo, chicha = (item["id"] for item in order["items"])

    cambiado = await client.patch(
        f"{ORDERS_URL}/{order['id']}/items/{lomo}",
        json={"quantity": 3, "notes": "término medio"},
        headers=mesero,
    )
    quitado = await client.delete(f"{ORDERS_URL}/{order['id']}/items/{chicha}", headers=mesero)
    assert cambiado.status_code == 200
    assert quitado.json()["total"] == "84.00"

    await _step(client, mesero, order["id"], "send")
    tarde = await client.patch(
        f"{ORDERS_URL}/{order['id']}/items/{lomo}", json={"quantity": 1}, headers=mesero
    )
    assert tarde.status_code == 409


async def test_editar_la_nota_no_reinicia_el_tiempo_en_el_estado(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero = authorization_for(local_a.waiter)
    order = await _open(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))
    assert order["status_changed_at"] == order["created_at"]
    enviado = await _step(client, mesero, order["id"], "send")

    editado = await client.patch(
        f"{ORDERS_URL}/{order['id']}", json={"notes": "sin cebolla"}, headers=mesero
    )

    assert editado.status_code == 200
    assert _moment(enviado["status_changed_at"]) > _moment(order["status_changed_at"])
    assert editado.json()["status_changed_at"] == enviado["status_changed_at"]
    assert _moment(editado.json()["updated_at"]) > _moment(enviado["updated_at"])


async def test_transicion_invalida_responde_409(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero = authorization_for(local_a.waiter)
    order = await _open(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))
    vacio = await _open(client, mesero, carta_a.mesa_2)

    servir = await client.post(f"{ORDERS_URL}/{order['id']}/served", headers=mesero)
    enviar_vacio = await client.post(f"{ORDERS_URL}/{vacio['id']}/send", headers=mesero)

    assert servir.status_code == 409
    assert enviar_vacio.status_code == 422


async def test_cancelar_exige_motivo_y_queda_en_la_bitacora(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    encargado = authorization_for(local_a.admin)
    order = await _open(
        client, authorization_for(local_a.waiter), carta_a.mesa_1, (carta_a.lomo, 1)
    )

    sin_motivo = await client.post(
        f"{ORDERS_URL}/{order['id']}/cancel", json={"reason": "   "}, headers=encargado
    )
    cancelado = await _step(client, encargado, order["id"], "cancel", reason="Cliente se fue")

    assert sin_motivo.status_code == 422
    assert cancelado["status"] == "cancelled"
    assert cancelado["cancel_reason"] == "Cliente se fue"
    bitacora = await client.get("/api/v1/activity", headers=encargado)
    assert bitacora.json()["items"][0]["kind"] == "order_cancelled"


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("ready", None),
        ("cancel", {"reason": "No quiero"}),
        ("charge", {"payment_method": "cash"}),
    ],
)
async def test_el_mesero_no_maneja_la_cocina_ni_la_caja(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    carta_a: Carta,
    action: str,
    body: dict[str, Any] | None,
) -> None:
    mesero = authorization_for(local_a.waiter)
    order = await _open(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))

    response = await client.post(f"{ORDERS_URL}/{order['id']}/{action}", json=body, headers=mesero)

    assert response.status_code == 403


async def test_el_mesero_ve_los_suyos_y_los_activos_el_encargado_todos(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    otro = await SqlAlchemyUserRepository(session).add(
        build_user(local_a.id, "carla@local-a.pe", full_name="Carla Ríos")
    )
    await session.commit()
    mesero, companera = authorization_for(local_a.waiter), authorization_for(otro)
    encargado = authorization_for(local_a.admin)

    cobrado = await _served_order(client, local_a, carta_a)
    await _step(client, encargado, cobrado["id"], "charge", payment_method="card")
    activo = await _open(client, mesero, carta_a.mesa_2, (carta_a.aji, 1))
    propio = await _open(client, companera, None, (carta_a.chicha, 2))

    listado = await client.get(ORDERS_URL, headers=companera)
    ajeno_cobrado = await client.get(f"{ORDERS_URL}/{cobrado['id']}", headers=companera)
    ajeno_activo = await client.get(f"{ORDERS_URL}/{activo['id']}", headers=companera)
    todos = await client.get(ORDERS_URL, headers=encargado)
    pagados = await client.get(ORDERS_URL, params={"status": "paid"}, headers=encargado)

    assert {o["id"] for o in listado.json()["items"]} == {activo["id"], propio["id"]}
    assert ajeno_cobrado.status_code == 404
    assert ajeno_activo.status_code == 200
    assert todos.json()["total"] == 3
    assert [o["id"] for o in pagados.json()["items"]] == [cobrado["id"]]


async def test_filtrar_por_fecha_del_local(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    order = await _open(client, authorization_for(local_a.waiter), None, (carta_a.lomo, 1))
    encargado = authorization_for(local_a.admin)

    hoy = await client.get(
        ORDERS_URL,
        params={"date_from": order["business_date"], "date_to": order["business_date"]},
        headers=encargado,
    )
    antes = await client.get(ORDERS_URL, params={"date_to": "2000-01-01"}, headers=encargado)

    assert hoy.json()["total"] == 1
    assert antes.json()["total"] == 0


async def test_el_tablero_trae_los_activos_en_orden_de_llegada(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    mesero = authorization_for(local_a.waiter)
    primero = await _open(client, mesero, carta_a.mesa_1, (carta_a.lomo, 1))
    segundo = await _open(client, mesero, carta_a.mesa_2, (carta_a.aji, 1))
    cancelado = await _open(client, mesero, None, (carta_a.chicha, 1))
    await _step(client, authorization_for(local_a.admin), cancelado["id"], "cancel", reason="x")

    response = await client.get(f"{ORDERS_URL}/active", headers=mesero)

    assert [o["id"] for o in response.json()] == [primero["id"], segundo["id"]]
    assert response.json()[0]["items"][0]["name"] == "Lomo saltado"


async def test_cada_cambio_avisa_al_tablero(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta, broker: LocalBroker
) -> None:
    async with broker.subscribe() as queue:
        order = await _open(client, authorization_for(local_a.waiter), None, (carta_a.lomo, 1))
        await _step(client, authorization_for(local_a.waiter), order["id"], "send")
        await asyncio.sleep(0)

        avisos = [queue.get_nowait(), queue.get_nowait()]

    assert {aviso.topic for aviso in avisos} == {"orders"}
    assert {aviso.reference_id for aviso in avisos} == {order["id"]}
    assert all(aviso.restaurant_id == local_a.id for aviso in avisos)
    assert all(aviso.roles == frozenset(Role) for aviso in avisos)


@pytest.mark.parametrize(
    ("method", "suffix", "body", "who"),
    [
        ("GET", "", None, "admin"),
        ("PATCH", "", {"notes": "intruso"}, "admin"),
        ("POST", "/items", {"items": [{"menu_item_id": 1}]}, "waiter"),
        ("POST", "/send", None, "waiter"),
        ("POST", "/ready", None, "admin"),
        ("POST", "/served", None, "waiter"),
        ("POST", "/charge", {"payment_method": "cash"}, "admin"),
        ("POST", "/cancel", {"reason": "intruso"}, "admin"),
    ],
)
async def test_un_local_no_ve_ni_toca_los_pedidos_de_otro(
    client: AsyncClient,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
    carta_b: Carta,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
    who: str,
) -> None:
    ajeno = await _open(
        client, authorization_for(local_b.waiter), carta_b.mesa_1, (carta_b.lomo, 1)
    )
    intruso = local_a.admin if who == "admin" else local_a.waiter

    response = await client.request(
        method, f"{ORDERS_URL}/{ajeno['id']}{suffix}", json=body, headers=authorization_for(intruso)
    )

    assert response.status_code == 404
    intacto = await client.get(
        f"{ORDERS_URL}/{ajeno['id']}", headers=authorization_for(local_b.admin)
    )
    assert intacto.json()["status"] == "open"
    assert intacto.json()["notes"] == ""
    listado = await client.get(ORDERS_URL, headers=authorization_for(local_a.admin))
    assert listado.json()["total"] == 0


async def test_no_se_abre_un_pedido_en_la_mesa_de_otro_local(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta, carta_b: Carta
) -> None:
    response = await client.post(
        ORDERS_URL,
        json={"type": "dine_in", "table_id": carta_b.mesa_1},
        headers=authorization_for(local_a.waiter),
    )

    assert response.status_code == 404
