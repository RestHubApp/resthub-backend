"""Caja y cobro por HTTP: turnos, cuenta dividida, pago mixto, descuentos y propinas."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.realtime_broker import LocalBroker
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from tests.builders import Carta, carta
from tests.conftest import StaffedRestaurant, authorization_for, build_user

ORDERS_URL = "/api/v1/orders"
CASH_URL = "/api/v1/cash"


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    return await carta(session, local_a.id)


def _admin(local: StaffedRestaurant) -> dict[str, str]:
    return authorization_for(local.admin)


def _waiter(local: StaffedRestaurant) -> dict[str, str]:
    return authorization_for(local.waiter)


async def _abrir_caja(
    client: AsyncClient, local: StaffedRestaurant, inicial: str = "100.00"
) -> dict[str, Any]:
    response = await client.post(
        f"{CASH_URL}/open", json={"opening_amount": inicial}, headers=_admin(local)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _servido(
    client: AsyncClient,
    local: StaffedRestaurant,
    menu: Carta,
    headers: dict[str, str] | None = None,
    items: tuple[tuple[int, int], ...] | None = None,
) -> dict[str, Any]:
    """Un pedido para llevar ya servido. Por omisión: 2 lomos y 1 chicha (61.50)."""
    mesero = headers or _waiter(local)
    lineas = items or ((menu.lomo, 2), (menu.chicha, 1))
    creado = await client.post(
        ORDERS_URL,
        json={
            "type": "takeaway",
            "items": [{"menu_item_id": dish, "quantity": q} for dish, q in lineas],
        },
        headers=mesero,
    )
    assert creado.status_code == 201, creado.text
    order_id = creado.json()["id"]
    for step, who in (("send", mesero), ("ready", _admin(local)), ("served", mesero)):
        response = await client.post(f"{ORDERS_URL}/{order_id}/{step}", headers=who)
        assert response.status_code == 200, response.text
    return response.json()


async def _pagar(
    client: AsyncClient, headers: dict[str, str], order_id: int, **body: Any
) -> dict[str, Any]:
    if "amount" in body and "expected_balance" not in body:
        # Como la pantalla de cobro: una parte libre va con el saldo que se vio.
        pedido = await client.get(f"{ORDERS_URL}/{order_id}", headers=headers)
        body["expected_balance"] = pedido.json()["balance"]
    response = await client.post(f"{ORDERS_URL}/{order_id}/payments", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# -- Turnos -------------------------------------------------------------------


async def test_sin_caja_abierta_no_se_cobra(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    servido = await _servido(client, local_a, carta_a)

    response = await client.post(
        f"{ORDERS_URL}/{servido['id']}/charge",
        json={"payment_method": "yape"},
        headers=_admin(local_a),
    )
    actual = await client.get(f"{CASH_URL}/current", headers=_waiter(local_a))

    assert response.status_code == 409
    assert "caja está cerrada" in response.json()["detail"]
    assert actual.json() == {"is_open": False, "session": None}


async def test_solo_el_encargado_abre_y_hay_una_sola_caja_abierta(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    mesero = await client.post(
        f"{CASH_URL}/open", json={"opening_amount": "50"}, headers=_waiter(local_a)
    )
    abierta = await _abrir_caja(client, local_a, "150.00")
    otra = await client.post(
        f"{CASH_URL}/open", json={"opening_amount": "10"}, headers=_admin(local_a)
    )
    # Cada local tiene su propia caja.
    del_otro_local = await _abrir_caja(client, local_b)

    assert mesero.status_code == 403
    assert abierta["is_open"] is True
    assert abierta["opening_amount"] == "150.00"
    assert abierta["opened_by_name"] == "Rosa Pérez"
    assert abierta["summary"]["expected_cash"] == "150.00"
    assert otra.status_code == 409
    assert del_otro_local["id"] != abierta["id"]


async def test_el_mesero_sabe_que_la_caja_esta_abierta_pero_no_ve_los_montos(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    await _abrir_caja(client, local_a)

    del_mesero = await client.get(f"{CASH_URL}/current", headers=_waiter(local_a))
    del_encargado = await client.get(f"{CASH_URL}/current", headers=_admin(local_a))
    historial = await client.get(f"{CASH_URL}/sessions", headers=_waiter(local_a))

    assert del_mesero.json() == {"is_open": True, "session": None}
    assert del_encargado.json()["session"]["opening_amount"] == "100.00"
    assert historial.status_code == 403


async def test_abrir_la_caja_avisa_a_todo_el_personal(
    client: AsyncClient, local_a: StaffedRestaurant, broker: LocalBroker
) -> None:
    async with broker.subscribe() as queue:
        abierta = await _abrir_caja(client, local_a)
        await asyncio.sleep(0)
        aviso = queue.get_nowait()

    assert aviso.topic == "cash"
    assert aviso.reference_id == abierta["id"]


# -- Quién cobra --------------------------------------------------------------


async def test_el_mesero_cobra_los_suyos_y_no_los_de_un_companero(
    client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    companera = await SqlAlchemyUserRepository(session).add(
        build_user(local_a.id, "carla@local-a.pe", local_a.waiter.role, full_name="Carla Ríos")
    )
    await session.commit()
    await _abrir_caja(client, local_a)
    propio = await _servido(client, local_a, carta_a)
    ajeno = await _servido(client, local_a, carta_a, headers=authorization_for(companera))

    cobrado = await client.post(
        f"{ORDERS_URL}/{propio['id']}/charge",
        json={"payment_method": "plin"},
        headers=_waiter(local_a),
    )
    rechazado = await client.post(
        f"{ORDERS_URL}/{ajeno['id']}/charge",
        json={"payment_method": "plin"},
        headers=_waiter(local_a),
    )
    del_encargado = await client.post(
        f"{ORDERS_URL}/{ajeno['id']}/charge",
        json={"payment_method": "card"},
        headers=_admin(local_a),
    )

    assert cobrado.status_code == 200
    assert cobrado.json()["payments"][0]["received_by_name"] == "Luis Torres"
    assert rechazado.status_code == 403
    assert del_encargado.status_code == 200
    assert del_encargado.json()["status"] == "paid"


async def test_un_doble_toque_no_cobra_dos_veces(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _abrir_caja(client, local_a)
    servido = await _servido(client, local_a, carta_a)
    url = f"{ORDERS_URL}/{servido['id']}/payments"
    parte = {"payment_method": "yape", "amount": "20.50", "expected_balance": "61.50"}

    primero = await client.post(url, json=parte, headers=_waiter(local_a))
    segundo = await client.post(url, json=parte, headers=_waiter(local_a))

    assert primero.status_code == 201
    assert primero.json()["balance"] == "41.00"
    assert segundo.status_code == 409
    assert "S/ 41.00" in segundo.json()["detail"]


# -- Cuenta dividida y pago mixto ----------------------------------------------


async def test_cuenta_dividida_en_partes_iguales_con_pago_mixto(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _abrir_caja(client, local_a)
    servido = await _servido(client, local_a, carta_a)
    mesero = _waiter(local_a)

    # 61.50 entre tres: 20.50 cada uno.
    uno = await _pagar(client, mesero, servido["id"], payment_method="cash", amount="20.50")
    dos = await _pagar(client, mesero, servido["id"], payment_method="yape", amount="20.50")
    cancelar = await client.post(
        f"{ORDERS_URL}/{servido['id']}/cancel",
        json={"reason": "Se fueron"},
        headers=_admin(local_a),
    )
    tres = await _pagar(client, mesero, servido["id"], payment_method="yape")

    assert uno["status"] == "served"
    assert uno["balance"] == "41.00"
    assert dos["paid_amount"] == "41.00"
    assert cancelar.status_code == 409
    assert tres["status"] == "paid"
    assert tres["balance"] == "0.00"
    assert tres["payments"][2]["amount"] == "20.50"
    assert tres["payment_method"] is None
    assert tres["payment_method_label"] == "Mixto"


async def test_cuenta_dividida_por_platos(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _abrir_caja(client, local_a)
    servido = await _servido(client, local_a, carta_a, items=((carta_a.lomo, 1), (carta_a.aji, 1)))
    lomo, aji = (item["id"] for item in servido["items"])
    mesero = _waiter(local_a)

    primero = await _pagar(client, mesero, servido["id"], payment_method="card", item_ids=[lomo])
    repetido = await client.post(
        f"{ORDERS_URL}/{servido['id']}/payments",
        json={"payment_method": "cash", "item_ids": [lomo]},
        headers=mesero,
    )
    segundo = await _pagar(
        client, mesero, servido["id"], payment_method="cash", item_ids=[aji], amount_received="30"
    )

    assert primero["payments"][0]["amount"] == "28.00"
    assert [item["is_paid"] for item in primero["items"]] == [True, False]
    assert repetido.status_code == 422
    assert segundo["status"] == "paid"
    assert segundo["payments"][1]["amount"] == "22.00"
    assert segundo["payments"][1]["change"] == "8.00"


async def test_una_parte_no_puede_pasar_lo_que_falta(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _abrir_caja(client, local_a)
    servido = await _servido(client, local_a, carta_a)

    response = await client.post(
        f"{ORDERS_URL}/{servido['id']}/payments",
        json={"payment_method": "yape", "amount": "70.00", "expected_balance": servido["balance"]},
        headers=_waiter(local_a),
    )

    assert response.status_code == 422


# -- Propinas -------------------------------------------------------------------


async def test_la_propina_va_aparte_de_la_venta(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _abrir_caja(client, local_a)
    servido = await _servido(client, local_a, carta_a)

    pagado = await client.post(
        f"{ORDERS_URL}/{servido['id']}/charge",
        json={"payment_method": "cash", "amount_received": "70.00", "tip": "5.00"},
        headers=_waiter(local_a),
    )
    corto = await _servido(client, local_a, carta_a)
    sin_cubrir = await client.post(
        f"{ORDERS_URL}/{corto['id']}/charge",
        json={"payment_method": "cash", "amount_received": "62.00", "tip": "5.00"},
        headers=_waiter(local_a),
    )

    body = pagado.json()
    assert body["total"] == "61.50"
    assert body["tips"] == "5.00"
    # Entregó 70: 61.50 de la cuenta, 5 de propina, 3.50 de vuelto.
    assert body["change"] == "3.50"
    assert sin_cubrir.status_code == 422


# -- Descuentos y cortesías -----------------------------------------------------


async def test_el_mesero_descuenta_hasta_su_tope_y_el_encargado_sin_tope(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    servido = await _servido(client, local_a, carta_a)
    url = f"{ORDERS_URL}/{servido['id']}/discount"

    sin_motivo = await client.put(url, json={"percent": "10"}, headers=_waiter(local_a))
    pasado = await client.put(
        url, json={"percent": "15", "reason": "Cliente frecuente"}, headers=_waiter(local_a)
    )
    dentro = await client.put(
        url, json={"percent": "10", "reason": "Cliente frecuente"}, headers=_waiter(local_a)
    )
    del_encargado = await client.put(
        url, json={"percent": "30", "reason": "Demora en cocina"}, headers=_admin(local_a)
    )

    assert sin_motivo.status_code == 422
    assert pasado.status_code == 403
    assert "10.00 %" in pasado.json()["detail"]
    assert dentro.json()["discount_amount"] == "6.15"
    assert dentro.json()["total"] == "55.35"
    assert del_encargado.json()["discount_percent"] == "30.00"
    assert del_encargado.json()["discount_amount"] == "18.45"
    assert del_encargado.json()["total"] == "43.05"
    assert del_encargado.json()["discounted_by_name"] == "Rosa Pérez"


async def test_el_encargado_cambia_el_tope_del_mesero(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    servido = await _servido(client, local_a, carta_a)
    tope = await client.patch(
        "/api/v1/restaurant", json={"max_waiter_discount_percent": "20"}, headers=_admin(local_a)
    )

    ahora = await client.put(
        f"{ORDERS_URL}/{servido['id']}/discount",
        json={"percent": "15", "reason": "Cumpleaños"},
        headers=_waiter(local_a),
    )

    assert tope.json()["max_waiter_discount_percent"] == "20.00"
    assert ahora.status_code == 200


async def test_las_cortesias_son_del_encargado_y_no_se_cobran(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _abrir_caja(client, local_a)
    servido = await _servido(client, local_a, carta_a)
    chicha = servido["items"][1]["id"]
    url = f"{ORDERS_URL}/{servido['id']}/items/{chicha}/courtesy"

    del_mesero = await client.put(url, json={"reason": "Invita la casa"}, headers=_waiter(local_a))
    invitada = await client.put(url, json={"reason": "Invita la casa"}, headers=_admin(local_a))
    deshecha = await client.delete(url, headers=_admin(local_a))
    otra_vez = await client.put(url, json={"reason": "Invita la casa"}, headers=_admin(local_a))
    await _pagar(client, _waiter(local_a), servido["id"], payment_method="yape", amount="10.00")
    tarde = await client.delete(url, headers=_admin(local_a))

    assert del_mesero.status_code == 403
    assert invitada.json()["items"][1]["is_courtesy"] is True
    assert invitada.json()["courtesy_amount"] == "5.50"
    assert invitada.json()["total"] == "56.00"
    assert deshecha.json()["total"] == "61.50"
    assert otra_vez.json()["total"] == "56.00"
    # Con un pago hecho, cambiar lo que se cobra ya no cuadraría.
    assert tarde.status_code == 409


# -- Cierre y arqueo ------------------------------------------------------------


async def test_el_cierre_arquea_por_medio_y_por_mesero(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _abrir_caja(client, local_a, "100.00")
    mesero = _waiter(local_a)
    uno = await _servido(client, local_a, carta_a)
    await client.put(
        f"{ORDERS_URL}/{uno['id']}/discount",
        json={"percent": "10", "reason": "Cliente frecuente"},
        headers=mesero,
    )
    # 55.35: 30 en efectivo con 2 de propina, el resto por Yape con 3 de propina.
    await _pagar(client, mesero, uno["id"], payment_method="cash", amount="30.00", tip="2.00")
    await _pagar(client, mesero, uno["id"], payment_method="yape", tip="3.00")
    dos = await _servido(client, local_a, carta_a, items=((carta_a.aji, 1),))
    await _pagar(client, _admin(local_a), dos["id"], payment_method="cash")

    cierre = await client.post(
        f"{CASH_URL}/close",
        json={"counted_cash": "150.00", "notes": "Faltó sencillo"},
        headers=_admin(local_a),
    )
    despues = await _servido(client, local_a, carta_a)
    sin_caja = await client.post(
        f"{ORDERS_URL}/{despues['id']}/charge", json={"payment_method": "yape"}, headers=mesero
    )
    historial = await client.get(f"{CASH_URL}/sessions", headers=_admin(local_a))
    detalle = await client.get(
        f"{CASH_URL}/sessions/{cierre.json()['id']}", headers=_admin(local_a)
    )
    bitacora = await client.get(
        "/api/v1/activity", params={"kind": "cash_closed"}, headers=_admin(local_a)
    )

    body = cierre.json()
    resumen = body["summary"]
    assert cierre.status_code == 200
    assert body["is_open"] is False
    assert [(m["method"], m["amount"], m["tips"]) for m in resumen["by_method"]] == [
        ("cash", "52.00", "2.00"),
        ("yape", "25.35", "3.00"),
    ]
    assert resumen["sales"] == "77.35"
    assert resumen["tips"] == "5.00"
    assert resumen["discounts"] == "6.15"
    assert resumen["discounted_orders"] == 1
    assert resumen["paid_orders"] == 2
    # 100 de inicial + 52 en efectivo + 2 de propina en efectivo.
    assert body["expected_cash"] == "154.00"
    assert body["counted_cash"] == "150.00"
    assert body["difference"] == "-4.00"
    # Toda la venta y las propinas son del mesero que atendió, aunque cobró el
    # encargado el segundo pedido.
    assert [(w["name"], w["sales"], w["tips"]) for w in resumen["by_waiter"]] == [
        ("Luis Torres", "77.35", "5.00")
    ]
    assert sin_caja.status_code == 409
    assert historial.json()["total"] == 1
    assert detalle.json()["summary"]["sales"] == "77.35"
    assert bitacora.json()["items"][0]["kind"] == "cash_closed"


async def test_no_se_cierra_una_caja_que_no_esta_abierta(
    client: AsyncClient, local_a: StaffedRestaurant
) -> None:
    response = await client.post(
        f"{CASH_URL}/close", json={"counted_cash": "0"}, headers=_admin(local_a)
    )

    assert response.status_code == 404


async def test_un_local_no_ve_los_turnos_de_otro(
    client: AsyncClient, local_a: StaffedRestaurant, local_b: StaffedRestaurant
) -> None:
    ajena = await _abrir_caja(client, local_b)

    detalle = await client.get(f"{CASH_URL}/sessions/{ajena['id']}", headers=_admin(local_a))
    actual = await client.get(f"{CASH_URL}/current", headers=_admin(local_a))

    assert detalle.status_code == 404
    assert actual.json()["is_open"] is False


# -- Panel ----------------------------------------------------------------------


async def test_el_panel_reparte_un_pago_mixto_y_separa_las_propinas(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    await _abrir_caja(client, local_a)
    servido = await _servido(client, local_a, carta_a)
    mesero = _waiter(local_a)
    await _pagar(client, mesero, servido["id"], payment_method="cash", amount="40.00", tip="4.00")
    await _pagar(client, mesero, servido["id"], payment_method="yape")

    medios = await client.get("/api/v1/insights/payments", headers=_admin(local_a))
    meseros = await client.get("/api/v1/insights/waiters", headers=_admin(local_a))
    resumen = await client.get("/api/v1/insights/summary", headers=_admin(local_a))

    assert [(m["method"], m["amount"]) for m in medios.json()["methods"]] == [
        ("cash", "40.00"),
        ("yape", "21.50"),
    ]
    assert meseros.json()["waiters"][0]["tips"] == "4.00"
    assert resumen.json()["sales"] == "61.50"
