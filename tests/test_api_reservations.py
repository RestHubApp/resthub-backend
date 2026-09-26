"""Reservas de mesa por HTTP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.builders import Carta, carta
from tests.conftest import StaffedRestaurant, authorization_for

RESERVATIONS_URL = "/api/v1/reservations"
LIMA = "-05:00"


@pytest.fixture
async def carta_a(session: AsyncSession, local_a: StaffedRestaurant) -> Carta:
    return await carta(session, local_a.id)


def _manana(hora: int) -> str:
    """Mañana a esa hora en Lima, como la escribiría el celular."""
    dia = (datetime.now(UTC) - timedelta(hours=5) + timedelta(days=1)).date()
    return f"{dia.isoformat()}T{hora:02d}:00:00{LIMA}"


async def _reservar(client: AsyncClient, local: StaffedRestaurant, **datos: Any) -> Any:
    body = {"customer_name": "Familia Ríos", "party_size": 6, "phone": "988777666", **datos}
    return await client.post(RESERVATIONS_URL, json=body, headers=authorization_for(local.waiter))


async def test_la_misma_mesa_no_se_reserva_dos_veces_a_la_vez(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    primera = await _reservar(client, local_a, reserved_for=_manana(20), table_id=carta_a.mesa_1)
    cruzada = await _reservar(
        client, local_a, reserved_for=_manana(21), table_id=carta_a.mesa_1, customer_name="Otro"
    )
    despues = await _reservar(
        client, local_a, reserved_for=_manana(22), table_id=carta_a.mesa_1, customer_name="Tarde"
    )
    otra_mesa = await _reservar(
        client, local_a, reserved_for=_manana(21), table_id=carta_a.mesa_2, customer_name="Otro"
    )

    assert primera.status_code == 201, primera.text
    assert cruzada.status_code == 409
    assert "Familia Ríos" in cruzada.json()["detail"]
    # Dos horas por omisión: a las 22 la mesa ya se liberó.
    assert despues.status_code == 201
    assert otra_mesa.status_code == 201


async def test_las_del_dia_en_la_zona_del_local_y_su_ciclo(
    client: AsyncClient, local_a: StaffedRestaurant, carta_a: Carta
) -> None:
    creada = await _reservar(client, local_a, reserved_for=_manana(23))
    dia = _manana(23)[:10]
    reserva_id = creada.json()["id"]

    del_dia = await client.get(
        RESERVATIONS_URL, params={"day": dia}, headers=authorization_for(local_a.waiter)
    )
    llegaron = await client.post(
        f"{RESERVATIONS_URL}/{reserva_id}/status",
        params={"value": "seated"},
        headers=authorization_for(local_a.waiter),
    )
    otra_vez = await client.post(
        f"{RESERVATIONS_URL}/{reserva_id}/status",
        params={"value": "no_show"},
        headers=authorization_for(local_a.waiter),
    )

    # 23:00 en Lima son las 04:00 UTC del día siguiente, pero cuenta el día local.
    assert [r["id"] for r in del_dia.json()] == [reserva_id]
    assert llegaron.json()["status_label"] == "Llegaron"
    assert otra_vez.status_code == 422


async def test_no_se_reserva_en_el_pasado_ni_en_una_mesa_ajena(
    client: AsyncClient,
    session: AsyncSession,
    local_a: StaffedRestaurant,
    local_b: StaffedRestaurant,
) -> None:
    ayer = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    ajena = await carta(session, local_b.id)

    pasada = await _reservar(client, local_a, reserved_for=ayer)
    sin_zona = await _reservar(client, local_a, reserved_for="2030-01-01T20:00:00")
    mesa_ajena = await _reservar(client, local_a, reserved_for=_manana(20), table_id=ajena.mesa_1)

    assert pasada.status_code == 422
    assert sin_zona.status_code == 422
    assert mesa_ajena.status_code == 404
